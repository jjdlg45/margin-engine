"""Segmentation (Phase 4): two strictly separate outputs.

1. Commercial segments: a decision tree on the normalized price index using
   CONTEXT FEATURES ONLY. Realized price, discount, cost and rebate columns are
   structurally excluded (leakage guard) — the segments describe who pays what,
   not what they paid.
2. Margin-driver tree: predicts pocket margin % and may use discount and cost
   features. Its output is labeled explanatory drivers and must never be used
   as segments that justify price increases.

Leaf constraints (spec 8.4): lines >= max(min_leaf_lines, pct of data), plus
distinct-customer and distinct-item minimums, enforced by iterative refitting.
Stability is measured by partition agreement (ARI) on bootstrap resamples,
never by comparing raw leaf ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl
from sklearn.metrics import adjusted_rand_score
from sklearn.tree import DecisionTreeRegressor, _tree

from margin_engine.config import Rules, TreeConfig
from margin_engine.labels import label

CONTEXT_FEATURES = [
    "industry",
    "size_band",
    "region",
    "channel",
    "contract_type",
    "item_family",
    "item_role",
    "order_size_band",
    "abc_class",
]
MARGIN_EXTRA_FEATURES = ["discount_pct_band", "rebate_rate_band", "cost_ratio_band"]


@dataclass
class HoldoutEvaluation:
    """How a segmentation fit on earlier periods does on later ones."""

    n_fit_periods: int
    n_eval_periods: int
    n_eval_pairs: int
    mae: float | None
    mae_baseline: float | None
    mae_without_same_period_features: float | None
    same_period_features: list[str]
    note: str


@dataclass
class TreeResult:
    kind: str  # "commercial_segments" | "margin_drivers"
    target: str
    feature_names: list[str]
    leaf_assignments: pl.DataFrame  # source_line_key -> leaf_id
    rules: list[dict[str, Any]]  # readable rule per leaf
    model: DecisionTreeRegressor
    out_of_sample_ari_vs_planted: float | None = None
    bootstrap_median_ari: float | None = None
    recommendation_change_share: float | None = None
    holdout: HoldoutEvaluation | None = None
    notes: list[str] = field(default_factory=list)


def _order_size_band(df: pl.DataFrame) -> pl.DataFrame:
    """Terciles of order size, cut on QUANTITY.

    It was cut on order net value until Run 6. Net value is price x quantity
    over the same period the commercial tree is explaining, so the band handed
    the tree a coarse copy of the price it was fitting (Finding 11). Quantity
    carries the same business meaning - how big an order is - without the
    price in it.
    """
    size_col = "quantity_norm" if "quantity_norm" in df.columns else "quantity"
    if size_col not in df.columns:
        return df.with_columns(pl.lit("unknown").alias("order_size_band"))
    order_qty = pl.col(size_col).sum().over("order_id")
    q = df.select(pl.col(size_col).sum().over("order_id").alias("_o"))["_o"]
    lo, hi = q.quantile(0.33), q.quantile(0.67)
    if lo is None or hi is None:
        return df.with_columns(pl.lit("unknown").alias("order_size_band"))
    return df.with_columns(
        pl.when(order_qty <= lo)
        .then(pl.lit("small"))
        .when(order_qty <= hi)
        .then(pl.lit("medium"))
        .otherwise(pl.lit("large"))
        .alias("order_size_band")
    )


def prepare_segmentation_frame(
    observations: pl.DataFrame, customers: pl.DataFrame | None, items: pl.DataFrame | None
) -> pl.DataFrame:
    """Join context attributes and derived bands onto flagged observations."""
    df = observations
    if customers is not None:
        keep = [
            c
            for c in ("customer_id", "industry", "region", "size_band", "contract_type")
            if c in customers.columns
        ]
        df = df.join(customers.select(keep), on="customer_id", how="left")
    if items is not None and "item_role" not in df.columns and "item_role" in items.columns:
        df = df.join(items.select("item_id", "item_role"), on="item_id", how="left")
    df = _order_size_band(df)
    for col in CONTEXT_FEATURES:
        if col not in df.columns:
            df = df.with_columns(pl.lit("unknown").alias(col))
        else:
            df = df.with_columns(pl.col(col).fill_null("unknown"))
    return df


def _leakage_guard(feature_names: list[str], rules: Rules) -> list[str]:
    """Refuse any commercial feature that carries what the tree is explaining.

    Three tests, because a name check on its own is a spell-checker: until
    Run 6 the guard passed `revenue_abc` - a feature built from outcome-period
    revenue, which is price x quantity - because its name contains none of the
    forbidden words (Finding 11). Since 60.4 every commercial feature declares
    its lineage, and the decisive test reads that declaration.

    1. The name still may not contain a forbidden term.
    2. Every feature must HAVE a lineage entry. An undeclared feature is
       refused rather than assumed innocent.
    3. A feature whose lineage says it embeds the realized price is refused,
       whatever it is called.

    Returns the features that are same-period but price-free, so the caller
    can report them instead of pretending they are not there.
    """
    sc = rules.segmentation
    forbidden = sc.forbidden_commercial_features
    bad = [f for f in feature_names if any(term in f.lower() for term in forbidden)]
    if bad:
        msg = (
            f"leakage guard: commercial segmentation features {bad} contain "
            f"forbidden terms {list(forbidden)}"
        )
        raise ValueError(msg)
    undeclared = [f for f in feature_names if f not in sc.feature_lineage]
    if undeclared:
        msg = (
            f"leakage guard: commercial segmentation features {undeclared} have no "
            "feature_lineage entry. Declare source columns, formula, reference "
            "window, whether the feature uses outcome-period data and whether it "
            "embeds the realized price (segmentation.feature_lineage)."
        )
        raise ValueError(msg)
    priced = [f for f in feature_names if sc.feature_lineage[f].embeds_realized_price]
    if priced:
        msg = (
            f"leakage guard: commercial segmentation features {priced} declare that "
            "they embed the realized price, which is the target of this tree. Use a "
            "lagged or price-free basis, or move the feature to the margin-driver tree."
        )
        raise ValueError(msg)
    return [f for f in feature_names if sc.feature_lineage[f].uses_outcome_period_data]


def _encode(df: pl.DataFrame, features: list[str]) -> tuple[np.ndarray, list[str]]:
    dummies = df.select(features).to_dummies()
    return dummies.to_numpy().astype(np.float64), dummies.columns


class TargetEncoder:
    """Shrunken target encoding for categorical features.

    One numeric column per feature (category -> shrunken mean of the target),
    so a single tree split can group several similar categories ("industry IN
    {construction, manufacturing, oem}") instead of burning one depth level per
    one-hot category. Encodings are fit on the fitting sample only.
    """

    def __init__(self, features: list[str], shrinkage_k: float = 20.0) -> None:
        self.features = features
        self.k = shrinkage_k
        self.maps: dict[str, dict[str, float]] = {}
        self.global_mean = 0.0

    def fit(self, df: pl.DataFrame, target: str) -> TargetEncoder:
        mean_val = df[target].mean()
        self.global_mean = float(mean_val) if isinstance(mean_val, (int, float)) else 0.0
        for f in self.features:
            grp = df.group_by(f).agg(pl.col(target).mean().alias("m"), pl.len().alias("n"))
            self.maps[f] = {
                str(r[f]): (r["n"] * r["m"] + self.k * self.global_mean) / (r["n"] + self.k)
                for r in grp.to_dicts()
            }
        return self

    def fit_cross_fitted(
        self, df: pl.DataFrame, target: str, *, n_folds: int = 5, seed: int = 0
    ) -> np.ndarray:
        """Fit the full-sample maps and return an OUT-OF-FOLD encoded matrix.

        An encoding fit on the rows it then encodes gives each category the
        mean of the very targets the tree is about to split on, so a thin
        category looks like a clean split of its own target values and the
        tree takes it (Run 6, Finding 11). The matrix returned here encodes
        every row from the OTHER folds only, so the split has to survive data
        it was not built from. `self.maps` stay full-sample, because at
        predict time there is no fold to hold out.

        Folds are a seeded permutation, so the same frame and seed give the
        same assignment on every run.

        Each fold's encoding is computed by SUBTRACTION rather than by
        refitting: the per-category totals are accumulated once with the fold
        as a second axis, and a fold's "fit on the others" is the column total
        minus that fold's own. The arithmetic is identical to refitting
        n_folds times - the test asserts that - but it costs one pass instead
        of n_folds, which matters because this runs on every bootstrap refit.
        """
        self.fit(df, target)
        n = df.height
        if n_folds < 2 or n < n_folds * 2:
            # Too few rows to hold any out: fall back to the in-sample
            # encoding rather than encode from almost nothing.
            return self.transform(df)
        rng = np.random.default_rng(seed)
        fold = rng.permutation(n) % n_folds
        y = df[target].to_numpy().astype(np.float64)

        # bincount, not np.add.at: the latter is unbuffered and dominates the
        # whole segmentation stage when it runs on every bootstrap refit.
        fold_sum = np.bincount(fold, weights=y, minlength=n_folds)
        fold_n = np.bincount(fold, minlength=n_folds).astype(np.float64)
        # The global mean a refit on the other folds would have used.
        rest_total = np.maximum(float(n) - fold_n, 1.0)
        rest_global = (float(y.sum()) - fold_sum) / rest_total

        out = np.empty((n, len(self.features)), dtype=np.float64)
        for j, f in enumerate(self.features):
            idx = df[f].cast(pl.String).cast(pl.Categorical).to_physical().to_numpy()
            n_cat = int(idx.max()) + 1 if idx.size else 0
            cell = idx * n_folds + fold
            size = n_cat * n_folds
            cat_fold_sum = np.bincount(cell, weights=y, minlength=size).reshape(n_cat, n_folds)
            cat_fold_n = (
                np.bincount(cell, minlength=size).astype(np.float64).reshape(n_cat, n_folds)
            )
            rest_sum = cat_fold_sum.sum(axis=1)[:, None] - cat_fold_sum
            rest_n = cat_fold_n.sum(axis=1)[:, None] - cat_fold_n
            denom = rest_n + self.k
            enc = np.divide(
                rest_sum + self.k * rest_global[None, :],
                denom,
                out=np.broadcast_to(rest_global[None, :], rest_n.shape).copy(),
                where=denom > 0,
            )
            out[:, j] = enc[idx, fold]
        return out

    def transform(self, df: pl.DataFrame) -> np.ndarray:
        cols = []
        for f in self.features:
            m = self.maps[f]
            cols.append(
                df[f].cast(pl.String).replace_strict(m, default=self.global_mean).to_numpy()
            )
        return np.column_stack(cols).astype(np.float64)

    def categories_below(self, feature: str, threshold: float) -> list[str]:
        return sorted(c for c, v in self.maps[feature].items() if v <= threshold)

    def categories_above(self, feature: str, threshold: float) -> list[str]:
        return sorted(c for c, v in self.maps[feature].items() if v > threshold)


def _pairs_for(work: pl.DataFrame, features: list[str]) -> pl.DataFrame:
    """One row per customer x item pair: median price index, modal context."""
    keys = ["customer_id", "item_id"]
    return (
        work.group_by(keys)
        .agg(
            pl.col("price_index").median().alias("price_index"),
            *[pl.col(f).sort().mode().sort().first().alias(f) for f in features],
        )
        .sort(keys)
    )


def _holdout_mae(
    fit_pairs: pl.DataFrame,
    eval_pairs: pl.DataFrame,
    features: list[str],
    tc: TreeConfig,
    folds: int,
    seed: int,
) -> float | None:
    """Mean absolute error of the fitted segment means on the later periods."""
    if not features or fit_pairs.height < 20 or eval_pairs.height == 0:
        return None
    enc = TargetEncoder(list(features))
    x = enc.fit_cross_fitted(fit_pairs, "price_index", n_folds=folds, seed=seed)
    model = DecisionTreeRegressor(
        max_depth=tc.max_depth,
        min_samples_leaf=max(20, int(0.02 * fit_pairs.height)),
        random_state=0,
    )
    model.fit(x, fit_pairs["price_index"].to_numpy())
    pred = model.predict(enc.transform(eval_pairs))
    return float(np.mean(np.abs(eval_pairs["price_index"].to_numpy() - pred)))


def time_based_holdout(
    work: pl.DataFrame, features: list[str], rules: Rules, *, seed: int = 0
) -> HoldoutEvaluation | None:
    """Fit on the earlier periods, score on the later ones (Run 6, Phase 76).

    A bootstrap resamples the same months, so it cannot tell a segmentation
    that will still hold next quarter from one that has fitted this quarter's
    noise. This splits on TIME instead: segments are grown on the first
    `holdout_fit_period_share` of calendar months and scored by how well their
    means predict the pairs in the later months, against the only baseline
    worth beating - one median for everybody.

    It runs twice: with every context feature, and again without the features
    whose lineage declares they come from the same period as the target. The
    difference is what those features are worth out of time, as a number
    rather than an argument.
    """
    if "invoice_date" not in work.columns:
        return None
    sc = rules.segmentation
    df = work.with_columns(
        (pl.col("invoice_date").dt.year() * 12 + pl.col("invoice_date").dt.month()).alias("_m")
    ).filter(pl.col("_m").is_not_null())
    periods = sorted(int(v) for v in df["_m"].unique().to_list())
    if len(periods) < 4:
        return HoldoutEvaluation(
            n_fit_periods=len(periods),
            n_eval_periods=0,
            n_eval_pairs=0,
            mae=None,
            mae_baseline=None,
            mae_without_same_period_features=None,
            same_period_features=[],
            note=(
                f"{len(periods)} periods of history is too short to hold later "
                "periods out; the time-based check is unavailable"
            ),
        )
    cut = max(1, min(len(periods) - 1, int(np.ceil(len(periods) * sc.holdout_fit_period_share))))
    fit_periods, eval_periods = periods[:cut], periods[cut:]
    fit_pairs = _pairs_for(df.filter(pl.col("_m") <= fit_periods[-1]), features)
    eval_pairs = _pairs_for(df.filter(pl.col("_m") > fit_periods[-1]), features)

    same_period = [
        f
        for f in features
        if f in sc.feature_lineage and sc.feature_lineage[f].uses_outcome_period_data
    ]
    lean = [f for f in features if f not in same_period]
    tc = sc.commercial_tree
    folds = sc.target_encoding_folds
    mae = _holdout_mae(fit_pairs, eval_pairs, features, tc, folds, seed)
    mae_lean = _holdout_mae(fit_pairs, eval_pairs, lean, tc, folds, seed)
    baseline = None
    if eval_pairs.height and fit_pairs.height:
        centre = float(np.median(fit_pairs["price_index"].to_numpy()))
        baseline = float(np.mean(np.abs(eval_pairs["price_index"].to_numpy() - centre)))
    if mae is None or baseline is None:
        note = "not enough pairs in the later periods to score the segmentation"
    elif mae < baseline:
        note = (
            f"segments fit on the first {len(fit_periods)} periods predict the later "
            f"{len(eval_periods)} better than a single median "
            f"({mae:.4f} against {baseline:.4f} mean absolute error)"
        )
    else:
        note = (
            f"segments fit on the first {len(fit_periods)} periods do NOT beat a single "
            f"median on the later {len(eval_periods)} "
            f"({mae:.4f} against {baseline:.4f} mean absolute error)"
        )
    return HoldoutEvaluation(
        n_fit_periods=len(fit_periods),
        n_eval_periods=len(eval_periods),
        n_eval_pairs=int(eval_pairs.height),
        mae=mae,
        mae_baseline=baseline,
        mae_without_same_period_features=mae_lean,
        same_period_features=same_period,
        note=note,
    )


def _min_leaf(df_height: int, tc: TreeConfig) -> int:
    return max(tc.min_leaf_lines, int(np.ceil(df_height * tc.min_leaf_lines_pct)))


def _node_parents(model: DecisionTreeRegressor) -> np.ndarray:
    tree = model.tree_
    parents = np.full(tree.node_count, -1, dtype=np.int64)
    for node in range(tree.node_count):
        left, right = tree.children_left[node], tree.children_right[node]
        if left != _tree.TREE_LEAF:
            parents[left] = node
            parents[right] = node
    return parents


def _valid_node_assignment(
    model: DecisionTreeRegressor, x: np.ndarray, df: pl.DataFrame, tc: TreeConfig
) -> np.ndarray:
    """Assign each sample to a tree node such that every ASSIGNED cell satisfies
    the line / distinct-customer / distinct-item minimums.

    Starts at the leaves and iteratively merges violating cells into their
    parent node until the partition is valid (prefix-closed by construction).
    The constraints are checked on the final assigned sets, not on pass-through
    membership — a parent that only receives a pruned subtree must itself
    satisfy the minimums or keep merging upward.
    """
    parents = _node_parents(model)
    assigned: np.ndarray = np.asarray(model.apply(x), dtype=np.int64)
    min_lines = _min_leaf(len(x), tc)
    cust = df["customer_id"].to_numpy()
    item = df["item_id"].to_numpy()
    for _ in range(int(model.get_depth()) + 2):
        cells = pl.DataFrame({"node": assigned, "c": cust, "i": item})
        stats = cells.group_by("node").agg(
            pl.len().alias("n"),
            pl.col("c").n_unique().alias("nc"),
            pl.col("i").n_unique().alias("ni"),
        )
        bad = stats.filter(
            (
                (pl.col("n") < min_lines)
                | (pl.col("nc") < tc.min_leaf_customers)
                | (pl.col("ni") < tc.min_leaf_items)
            )
            & (pl.col("node") != 0)
        )["node"].to_list()
        if not bad:
            break
        remap = {node: int(parents[node]) if parents[node] != -1 else 0 for node in bad}
        assigned = np.array([remap.get(int(a), int(a)) for a in assigned], dtype=np.int64)
    return assigned


def _fit_constrained(
    x: np.ndarray, y: np.ndarray, df: pl.DataFrame, tc: TreeConfig, seed: int = 0
) -> tuple[DecisionTreeRegressor, np.ndarray, list[str]]:
    """Fit at the configured line minimum with validation-selected pruning,
    then post-prune any cells violating the distinct-customer/item minimums
    into their parent nodes."""
    notes: list[str] = []
    min_leaf = _min_leaf(len(y), tc)
    # No CV pruning here: the shallow depth and the configured line/customer/
    # item minimums bound complexity, and CV alpha selection costs dozens of
    # full fits on line-level data (the commercial tree has its own
    # consolidation mechanism at pair level).
    model = DecisionTreeRegressor(
        max_depth=tc.max_depth, min_samples_leaf=min_leaf, random_state=seed
    )
    model.fit(x, y)
    raw_leaves = model.apply(x)
    assigned = _valid_node_assignment(model, x, df, tc)
    n_pruned = int((assigned != raw_leaves).sum())
    if n_pruned:
        notes.append(
            f"{len(set(raw_leaves) - set(assigned))} leaf/leaves below distinct "
            f"customer/item minimums merged into their parent nodes "
            f"({n_pruned} lines reassigned)"
        )
    return model, assigned, notes


def _extract_rules(
    model: DecisionTreeRegressor, feature_names: list[str], df: pl.DataFrame, leaves: np.ndarray
) -> list[dict[str, Any]]:
    """Readable rule per leaf: the conjunction of one-hot splits on its path."""
    tree = model.tree_
    paths: dict[int, list[str]] = {}

    def walk(node: int, conditions: list[str]) -> None:
        # Record every node's path: post-pruned assignments may point at
        # internal nodes, whose rule is simply the path prefix.
        paths[node] = conditions
        if tree.children_left[node] == _tree.TREE_LEAF:
            return
        feat = feature_names[tree.feature[node]]
        # One-hot features: threshold 0.5 means "is not X" (left) / "is X" (right).
        walk(tree.children_left[node], [*conditions, f"NOT {feat}"])
        walk(tree.children_right[node], [*conditions, feat])

    walk(0, [])
    stats = (
        df.with_columns(pl.Series("_leaf", leaves))
        .group_by("_leaf")
        .agg(
            pl.len().alias("n_lines"),
            pl.col("customer_id").n_unique().alias("n_customers"),
            pl.col("item_id").n_unique().alias("n_items"),
        )
    )
    stats_map = {r["_leaf"]: r for r in stats.to_dicts()}
    out = []
    for leaf_id, conds in sorted(paths.items()):
        s = stats_map.get(leaf_id)
        if s is None:
            continue  # leaf unused by this sample
        # A cell at an internal node holds lines merged UP from leaves below
        # the reporting minimums: its path conditions hold for those lines but
        # do not uniquely describe them, so the rule must say so - a root cell
        # is NOT "all lines" unless it truly holds every line.
        is_internal = tree.children_left[leaf_id] != _tree.TREE_LEAF
        if is_internal and s["n_lines"] < len(leaves):
            qualifier = "[not assigned to a more specific leaf]"
            rule = (" AND ".join(conds) + " " + qualifier) if conds else qualifier
        else:
            rule = " AND ".join(conds) if conds else "(all lines)"
        out.append(
            {
                "leaf_id": int(leaf_id),
                "rule": rule,
                "n_lines": s["n_lines"],
                "n_customers": s["n_customers"],
                "n_items": s["n_items"],
                "mean_target": float(tree.value[leaf_id][0][0]),
            }
        )
    return out


def _extract_rules_encoded(
    model: DecisionTreeRegressor,
    encoder: TargetEncoder,
    df: pl.DataFrame,
    assigned: np.ndarray,
) -> list[dict[str, Any]]:
    """Readable set-membership rule per assigned cell for target-encoded trees:
    'industry IN {facilities, retail} AND size_band IN {small, medium}'."""
    tree = model.tree_
    all_cats = {f: set(encoder.maps[f].keys()) for f in encoder.features}
    paths: dict[int, dict[str, set[str]]] = {}

    def walk(node: int, allowed: dict[str, set[str]]) -> None:
        paths[node] = {f: set(s) for f, s in allowed.items()}
        if tree.children_left[node] == _tree.TREE_LEAF:
            return
        f = encoder.features[tree.feature[node]]
        t = float(tree.threshold[node])
        below = {c for c in allowed[f] if encoder.maps[f].get(c, encoder.global_mean) <= t}
        above = allowed[f] - below
        left_allowed = {**{k: set(v) for k, v in allowed.items()}, f: below}
        right_allowed = {**{k: set(v) for k, v in allowed.items()}, f: above}
        walk(int(tree.children_left[node]), left_allowed)
        walk(int(tree.children_right[node]), right_allowed)

    walk(0, all_cats)
    stats = (
        df.with_columns(pl.Series("_cell", assigned))
        .group_by("_cell")
        .agg(
            pl.len().alias("n_lines"),
            pl.col("customer_id").n_unique().alias("n_customers"),
            pl.col("item_id").n_unique().alias("n_items"),
            pl.col("price_index").median().alias("median_price_index"),
        )
    )
    out = []
    for s in stats.sort("_cell").to_dicts():
        cell = int(s["_cell"])
        allowed = paths.get(cell, all_cats)
        conds = []
        for f in encoder.features:
            if allowed[f] != all_cats[f] and allowed[f]:
                shown = sorted(allowed[f])
                if len(shown) <= len(all_cats[f]) / 2:
                    conds.append(f"{f} IN {{{', '.join(shown)}}}")
                else:
                    excluded = sorted(all_cats[f] - allowed[f])
                    conds.append(f"{f} NOT IN {{{', '.join(excluded)}}}")
        out.append(
            {
                "leaf_id": cell,
                "rule": " AND ".join(conds) if conds else "(all lines)",
                "n_lines": s["n_lines"],
                "n_customers": s["n_customers"],
                "n_items": s["n_items"],
                "median_price_index": s["median_price_index"],
            }
        )
    return out


def _consolidate_by_mean(leaves: np.ndarray, y: np.ndarray, threshold: float) -> dict[int, int]:
    """Merge partition cells whose (weighted) mean targets are closer than the
    commercial-distinctness threshold. CHAID-style: cells reached by different
    branches but priced the same are one segment. Returns leaf -> cell id."""
    uniq = np.unique(leaves)
    means = {int(u): float(y[leaves == u].mean()) for u in uniq}
    counts = {int(u): int((leaves == u).sum()) for u in uniq}
    groups: dict[int, list[int]] = {int(u): [int(u)] for u in uniq}
    while len(groups) > 1:
        ids = sorted(groups)
        best: tuple[float, int, int] | None = None
        for a_pos in range(len(ids)):
            for b_pos in range(a_pos + 1, len(ids)):
                a, b = ids[a_pos], ids[b_pos]
                d = abs(means[a] - means[b])
                if best is None or d < best[0]:
                    best = (d, a, b)
        if best is None or best[0] >= threshold:
            break
        _, a, b = best
        na, nb = counts[a], counts[b]
        means[a] = (means[a] * na + means[b] * nb) / (na + nb)
        counts[a] = na + nb
        groups[a].extend(groups[b])
        del groups[b], means[b], counts[b]
    out: dict[int, int] = {}
    for cell, members in groups.items():
        for leaf in members:
            out[leaf] = cell
    return out


def _enforce_minimums_pairs(
    pairs: pl.DataFrame, cells: np.ndarray, tc: TreeConfig, total_lines: int
) -> np.ndarray:
    """Pair-level constraint merge: cells violating the LINE-count / distinct
    customer / distinct item minimums merge into the nearest-median cell.
    Line counts come from the pairs' n_lines weights."""
    assigned = cells.copy()
    min_lines = max(tc.min_leaf_lines, int(np.ceil(total_lines * tc.min_leaf_lines_pct)))
    frame_base = pairs.select("customer_id", "item_id", "n_lines", "price_index")
    for _ in range(64):
        frame = frame_base.with_columns(pl.Series("cell", assigned))
        stats = frame.group_by("cell").agg(
            pl.col("n_lines").sum().alias("n"),
            pl.col("customer_id").n_unique().alias("nc"),
            pl.col("item_id").n_unique().alias("ni"),
            pl.col("price_index").median().alias("p50"),
        )
        if stats.height <= 1:
            break
        bad = stats.filter(
            (pl.col("n") < min_lines)
            | (pl.col("nc") < tc.min_leaf_customers)
            | (pl.col("ni") < tc.min_leaf_items)
        )
        if bad.height == 0:
            break
        worst = bad.sort("n").to_dicts()[0]
        others = stats.filter(pl.col("cell") != worst["cell"])
        target = (
            others.with_columns((pl.col("p50") - worst["p50"]).abs().alias("d"))
            .sort("d")
            .to_dicts()[0]["cell"]
        )
        assigned[assigned == worst["cell"]] = target
    return assigned


def _extract_rules_merged(
    model: DecisionTreeRegressor,
    encoder: TargetEncoder,
    cell_of: dict[int, int],
    df: pl.DataFrame,
    assigned: np.ndarray,
) -> list[dict[str, Any]]:
    """Readable rules for consolidated cells: OR of member-leaf rules."""
    tree = model.tree_
    all_cats = {f: set(encoder.maps[f].keys()) for f in encoder.features}
    paths: dict[int, dict[str, set[str]]] = {}

    def walk(node: int, allowed: dict[str, set[str]]) -> None:
        paths[node] = {f: set(s) for f, s in allowed.items()}
        if tree.children_left[node] == _tree.TREE_LEAF:
            return
        f = encoder.features[tree.feature[node]]
        t = float(tree.threshold[node])
        below = {c for c in allowed[f] if encoder.maps[f].get(c, encoder.global_mean) <= t}
        left_allowed = {**{k: set(v) for k, v in allowed.items()}, f: below}
        right_allowed = {**{k: set(v) for k, v in allowed.items()}, f: allowed[f] - below}
        walk(int(tree.children_left[node]), left_allowed)
        walk(int(tree.children_right[node]), right_allowed)

    walk(0, all_cats)

    def rule_str(allowed: dict[str, set[str]]) -> str:
        conds = []
        for f in encoder.features:
            if allowed[f] != all_cats[f] and allowed[f]:
                shown = sorted(allowed[f])
                if len(shown) <= len(all_cats[f]) / 2:
                    conds.append(f"{f} IN {{{', '.join(shown)}}}")
                else:
                    excluded = sorted(all_cats[f] - allowed[f])
                    conds.append(f"{f} NOT IN {{{', '.join(excluded)}}}")
        return " AND ".join(conds) if conds else "(all lines)"

    members: dict[int, list[int]] = {}
    for leaf, cell in cell_of.items():
        members.setdefault(cell, []).append(leaf)
    stats = (
        df.with_columns(pl.Series("_cell", assigned))
        .group_by("_cell")
        .agg(
            pl.len().alias("n_lines"),
            pl.col("customer_id").n_unique().alias("n_customers"),
            pl.col("item_id").n_unique().alias("n_items"),
            pl.col("price_index").median().alias("median_price_index"),
        )
    )
    out = []
    for s in stats.sort("median_price_index").to_dicts():
        cell = int(s["_cell"])
        leaf_list = members.get(cell, [cell])
        # Every member leaf's conditions stay in the rule (deduplicated, never
        # truncated): the plain-English name and description must cover every
        # branch of a merged segment.
        clauses = list(dict.fromkeys(rule_str(paths[leaf]) for leaf in leaf_list if leaf in paths))
        rule = " OR ".join(f"({c})" for c in clauses)
        out.append(
            {
                "leaf_id": cell,
                "rule": rule or "(all lines)",
                "n_lines": s["n_lines"],
                "n_customers": s["n_customers"],
                "n_items": s["n_items"],
                "median_price_index": s["median_price_index"],
            }
        )
    return out


def fit_commercial_segments(
    df: pl.DataFrame,
    rules: Rules,
    *,
    planted_segment_col: str | None = None,
    seed: int = 0,
) -> TreeResult:
    """Commercial segments on the price index, context features only.

    The tree is fit at the customer x item pair level (median price index per
    pair): the commercial structure lives on pairs, and line-level fitting
    would pseudo-replicate high-volume pairs and destabilize the greedy splits.
    Categorical features enter via shrunken target encoding so one split can
    group similar categories (standard CART treatment of categoricals); the
    resulting rules remain pure context-set memberships. Line-count/customer/
    item leaf minimums are enforced on the resulting line-level partition.
    """
    tc = rules.segmentation.commercial_tree
    work = df.filter(pl.col("price_index").is_not_null() & pl.col("price_index").is_finite())
    notes_pre: list[str] = []
    if "contract_id" in work.columns:
        n_contract = work.filter(pl.col("contract_id").is_not_null()).height
        if n_contract:
            notes_pre.append(
                f"{n_contract} contract-priced lines excluded from commercial "
                f"segmentation: their prices are governed by contracts, not "
                f"market segments (they keep their contract regime in guidance)"
            )
            work = work.filter(pl.col("contract_id").is_null())

    pair_keys = ["customer_id", "item_id"]
    # Sorted: group_by row order is otherwise nondeterministic, and the
    # train/test split and bootstrap draws index into this frame — unsorted,
    # the whole segmentation would vary run to run with the same seed.
    pairs = (
        work.group_by(pair_keys)
        .agg(
            pl.col("price_index").median().alias("price_index"),
            pl.len().alias("n_lines"),
            *[pl.col(f).sort().mode().sort().first().alias(f) for f in CONTEXT_FEATURES],
        )
        .sort(pair_keys)
    )
    same_period_features = _leakage_guard(CONTEXT_FEATURES, rules)
    if same_period_features:
        one = len(same_period_features) == 1
        notes_pre.append(
            ("context feature " if one else "context features ")
            + ", ".join(label(f).lower() for f in same_period_features)
            + (" is" if one else " are")
            + " measured over the same period as the price index it helps explain. "
            + ("It carries" if one else "They carry")
            + " no realized price - the leakage guard refuses any feature that "
            "does - but the time-based check reports what the feature is worth "
            "out of time"
        )
    names = list(CONTEXT_FEATURES)
    pairs["price_index"].to_numpy()

    # Pair-level tree-growth minimum: 2% of pairs, so leaf means are precise
    # enough (se ~ sd/sqrt(n)) for the consolidation threshold to separate
    # real segments from noise splits. The configured line/customer/item
    # minimums are enforced after mapping the partition back to lines.
    pair_tc = tc.model_copy(
        update={
            "min_leaf_lines": max(50, int(0.02 * pairs.height)),
            "min_leaf_lines_pct": 0.02,
            "min_leaf_customers": 1,
            "min_leaf_items": 1,
        }
    )

    rng = np.random.default_rng(seed)
    n = pairs.height
    test_mask = rng.random(n) < 0.25
    threshold = rules.segmentation.segment_merge_threshold
    pair_min = pair_tc.min_leaf_lines

    total_lines = int(pairs["n_lines"].sum())

    def fit_partition(
        fit_idx: np.ndarray,
    ) -> tuple[DecisionTreeRegressor, TargetEncoder, dict[int, int], np.ndarray]:
        """Grow tree on the given pairs, consolidate commercially
        indistinguishable leaves, enforce minimums — all at pair level
        (line counts carried as weights), so bootstraps stay cheap."""
        fit_pairs = pairs[fit_idx.tolist()]
        enc = TargetEncoder(names)
        # Out-of-fold encoding while the tree is grown (Run 6, Phase 76): an
        # in-sample encoding hands each category the mean of the targets the
        # split is about to be scored on. enc.maps stay full-sample, because
        # assignment and prediction have no fold to hold out.
        x_fit = enc.fit_cross_fitted(
            fit_pairs,
            "price_index",
            n_folds=rules.segmentation.target_encoding_folds,
            seed=seed,
        )
        m = DecisionTreeRegressor(max_depth=tc.max_depth, min_samples_leaf=pair_min, random_state=0)
        m.fit(x_fit, fit_pairs["price_index"].to_numpy())
        # Consolidation means computed on the fitting pairs only.
        fit_leaves = m.apply(enc.transform(fit_pairs))
        cell_of = _consolidate_by_mean(fit_leaves, fit_pairs["price_index"].to_numpy(), threshold)
        all_leaves = m.apply(enc.transform(pairs))
        cells = np.array([cell_of.get(int(leaf), int(leaf)) for leaf in all_leaves], dtype=np.int64)
        cells = _enforce_minimums_pairs(pairs, cells, tc, total_lines)
        return m, enc, cell_of, cells

    train_idx = np.flatnonzero(~test_mask)
    model, encoder, cell_of_full, full_cells = fit_partition(train_idx)
    notes = list(notes_pre)

    pair_assign = pairs.select(*pair_keys).with_columns(pl.Series("_cell_valid", full_cells))
    lines_full = work.join(pair_assign, on=pair_keys, how="left")
    full_leaves = lines_full["_cell_valid"].to_numpy()

    ari = None
    if planted_segment_col is not None and planted_segment_col in lines_full.columns:
        test_pairs = pairs.filter(pl.Series(test_mask)).select(pair_keys)
        test_lines = lines_full.join(test_pairs, on=pair_keys, how="semi")
        ari = float(
            adjusted_rand_score(
                test_lines[planted_segment_col].to_numpy(),
                test_lines["_cell_valid"].to_numpy(),
            )
        )

    # Bootstrap stability at pair level (weighted by nothing: each pair counts
    # once; the partition is defined on pairs).
    n_boot = rules.segmentation.bootstrap_resamples
    y_pairs = pairs["price_index"].to_numpy()
    p50_full_map = (
        pairs.with_columns(pl.Series("_c", full_cells))
        .group_by("_c")
        .agg(pl.col("price_index").median().alias("p50"))
    )
    full_target = dict(p50_full_map.iter_rows())
    target_full = np.array([full_target[c] for c in full_cells])
    aris: list[float] = []
    changed_shares: list[float] = []
    for _b in range(n_boot):
        idx = rng.integers(0, n, n)
        _, _, _, bcells = fit_partition(idx)
        aris.append(float(adjusted_rand_score(full_cells, bcells)))
        bmap = (
            pairs.with_columns(pl.Series("_c", bcells))
            .group_by("_c")
            .agg(pl.col("price_index").median().alias("p50"))
        )
        bdict = dict(bmap.iter_rows())
        btarget = np.array([bdict[c] for c in bcells])
        changed_shares.append(float(np.mean(np.abs(btarget - target_full) > 0.02)))

    assignments = lines_full.select("source_line_key").with_columns(
        pl.Series("segment_leaf", full_leaves.astype(np.int64))
    )
    _ = y_pairs
    holdout = time_based_holdout(work, names, rules, seed=seed)
    if holdout is not None:
        notes.append(holdout.note)
        if (
            holdout.mae is not None
            and holdout.mae_without_same_period_features is not None
            and holdout.same_period_features
        ):
            delta = holdout.mae_without_same_period_features - holdout.mae
            direction = "worse" if delta > 0 else "better"
            notes.append(
                "dropping the same-period features ("
                + ", ".join(label(f).lower() for f in holdout.same_period_features)
                + f") makes the later-period error {abs(delta):.4f} {direction}"
            )
    return TreeResult(
        kind="commercial_segments",
        target="price_index",
        feature_names=names,
        leaf_assignments=assignments,
        rules=_extract_rules_merged(model, encoder, cell_of_full, lines_full, full_leaves),
        model=model,
        out_of_sample_ari_vs_planted=ari,
        bootstrap_median_ari=float(np.median(aris)) if aris else None,
        recommendation_change_share=float(np.mean(changed_shares)) if changed_shares else None,
        holdout=holdout,
        notes=notes,
    )


def fit_margin_driver_tree(df: pl.DataFrame, rules: Rules, *, seed: int = 0) -> TreeResult:
    """Margin-driver tree: pocket margin %, context + discount/cost features.
    Output is explanatory, never a pricing segmentation."""
    tc = rules.segmentation.margin_driver_tree
    disc = pl.col("discount_cents")
    work = df.filter(
        (pl.col("pocket_revenue_cents") > 0)
        & pl.col("pocket_gp_cents").is_not_null()
        & pl.col("gross_list_value_cents").is_not_null()
    ).with_columns(
        (pl.col("pocket_gp_cents") / pl.col("pocket_revenue_cents")).alias("pocket_margin_pct"),
        (disc / pl.col("gross_list_value_cents").clip(lower_bound=1)).alias("_disc_pct"),
        (pl.col("rebate_actual_cents") / pl.col("net_amount_cents").clip(lower_bound=1)).alias(
            "_reb_rate"
        ),
        (pl.col("product_cost_cents") / pl.col("net_amount_cents").clip(lower_bound=1)).alias(
            "_cost_ratio"
        ),
    )
    work = work.filter(pl.col("pocket_margin_pct").is_finite())
    if work.height < 2:
        # No usable lines (e.g. no list price reference, so gross list value is
        # null everywhere): the module switches OFF with a stated reason
        # instead of crashing - the spec's optional-module policy (27.7 made
        # this reachable: a world with no reference tables at all).
        return TreeResult(
            kind="margin_drivers",
            target="pocket_margin_pct",
            feature_names=[],
            leaf_assignments=pl.DataFrame(
                schema={"source_line_key": pl.String, "driver_leaf": pl.Int64}
            ),
            rules=[],
            model=DecisionTreeRegressor(),
            notes=[
                "margin-driver tree OFF: no lines with positive pocket revenue, "
                "known pocket GP and a gross list value (is the price list "
                "reference table missing?)"
            ],
        )

    def band(col: str, name: str) -> pl.Expr:
        q1 = work[col].quantile(0.25)
        q3 = work[col].quantile(0.75)
        return (
            pl.when(pl.col(col) <= q1)
            .then(pl.lit("low"))
            .when(pl.col(col) <= q3)
            .then(pl.lit("mid"))
            .otherwise(pl.lit("high"))
            .alias(name)
        )

    work = work.with_columns(
        band("_disc_pct", "discount_pct_band"),
        band("_reb_rate", "rebate_rate_band"),
        band("_cost_ratio", "cost_ratio_band"),
    )
    features = CONTEXT_FEATURES + MARGIN_EXTRA_FEATURES
    x, names = _encode(work, features)
    y = work["pocket_margin_pct"].to_numpy()
    model, leaves, notes = _fit_constrained(x, y, work, tc, seed)
    assignments = work.select("source_line_key").with_columns(
        pl.Series("driver_leaf", leaves.astype(np.int64))
    )
    notes.append(
        "outputs are explanatory drivers of pocket margin, not segments that "
        "justify price increases"
    )
    return TreeResult(
        kind="margin_drivers",
        target="pocket_margin_pct",
        feature_names=names,
        leaf_assignments=assignments,
        rules=_extract_rules(model, names, work, leaves),
        model=model,
        notes=notes,
    )
