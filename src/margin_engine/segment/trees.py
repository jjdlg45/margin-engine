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

CONTEXT_FEATURES = [
    "industry",
    "size_band",
    "region",
    "channel",
    "contract_type",
    "item_family",
    "item_role",
    "order_size_band",
    "revenue_abc",
]
MARGIN_EXTRA_FEATURES = ["discount_pct_band", "rebate_rate_band", "cost_ratio_band"]


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
    notes: list[str] = field(default_factory=list)


def _order_size_band(df: pl.DataFrame) -> pl.DataFrame:
    order_net = pl.col("net_amount_cents").sum().over("order_id")
    q = df.select(pl.col("net_amount_cents").sum().over("order_id").alias("_o"))["_o"]
    lo, hi = q.quantile(0.33), q.quantile(0.67)
    return df.with_columns(
        pl.when(order_net <= lo)
        .then(pl.lit("small"))
        .when(order_net <= hi)
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
    if items is not None and "item_role" not in df.columns:
        df = df.join(items.select("item_id", "item_role"), on="item_id", how="left")
    df = _order_size_band(df)
    for col in CONTEXT_FEATURES:
        if col not in df.columns:
            df = df.with_columns(pl.lit("unknown").alias(col))
        else:
            df = df.with_columns(pl.col(col).fill_null("unknown"))
    return df


def _leakage_guard(feature_names: list[str], rules: Rules) -> None:
    forbidden = rules.segmentation.forbidden_commercial_features
    bad = [f for f in feature_names if any(term in f.lower() for term in forbidden)]
    if bad:
        msg = (
            f"leakage guard: commercial segmentation features {bad} contain "
            f"forbidden terms {list(forbidden)}"
        )
        raise ValueError(msg)


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


def _select_ccp_alpha(
    x: np.ndarray, y: np.ndarray, min_leaf: int, max_depth: int, seed: int
) -> float:
    """Cost-complexity pruning strength by 5-fold cross-validation with a
    1-SE rule: the largest alpha whose CV error is within one standard error
    (across folds) of the minimum. Prunes noise splits that would fragment
    real segments, and is stable because no single random split decides."""
    rng = np.random.default_rng(seed)
    n = len(y)
    base = DecisionTreeRegressor(max_depth=max_depth, min_samples_leaf=min_leaf, random_state=0)
    path = base.cost_complexity_pruning_path(x, y)
    pos = np.unique(path.ccp_alphas[path.ccp_alphas > 0])
    if len(pos) == 0:
        return 0.0
    # Log-spaced candidates across the positive range: path alphas span several
    # orders of magnitude and index-based subsampling misses the critical scale.
    alphas = np.concatenate([[0.0], np.geomspace(pos.min(), pos.max(), 14)])
    folds = rng.integers(0, 5, n)
    fold_mse = np.zeros((len(alphas), 5))
    for k in range(5):
        tr, va = folds != k, folds == k
        if va.sum() == 0 or tr.sum() == 0:
            continue
        for ai, a in enumerate(alphas):
            m = DecisionTreeRegressor(
                max_depth=max_depth, min_samples_leaf=min_leaf, random_state=0, ccp_alpha=float(a)
            )
            m.fit(x[tr], y[tr])
            fold_mse[ai, k] = float(((m.predict(x[va]) - y[va]) ** 2).mean())
    cv_mean = fold_mse.mean(axis=1)
    cv_se = fold_mse.std(axis=1) / np.sqrt(5)
    best = int(np.argmin(cv_mean))
    threshold = cv_mean[best] + cv_se[best]
    eligible = [float(alphas[i]) for i in range(len(alphas)) if cv_mean[i] <= threshold]

    # Among statistically equivalent alphas (within 1 SE of the CV minimum),
    # choose the one whose partitions are most stable under resampling: fit a
    # handful of bootstrap trees per alpha and score mean pairwise ARI of the
    # induced partitions. Stability is a first-class requirement (spec 8.8),
    # so it drives the tie-break, not the accuracy threshold.
    if len(eligible) == 1:
        return eligible[0]
    n_stab = 6
    best_alpha, best_score = eligible[0], -1.0
    for a in eligible:
        parts = []
        for _s in range(n_stab):
            idx = rng.integers(0, n, n)
            m = DecisionTreeRegressor(
                max_depth=max_depth, min_samples_leaf=min_leaf, random_state=0, ccp_alpha=a
            )
            m.fit(x[idx], y[idx])
            parts.append(m.apply(x))
        scores = [
            adjusted_rand_score(parts[i], parts[j])
            for i in range(n_stab)
            for j in range(i + 1, n_stab)
        ]
        score = float(np.mean(scores))
        if score > best_score + 1e-9 or (abs(score - best_score) <= 1e-9 and a > best_alpha):
            best_alpha, best_score = a, score
    return best_alpha


def _fit_constrained(
    x: np.ndarray, y: np.ndarray, df: pl.DataFrame, tc: TreeConfig, seed: int = 0
) -> tuple[DecisionTreeRegressor, np.ndarray, list[str]]:
    """Fit at the configured line minimum with validation-selected pruning,
    then post-prune any cells violating the distinct-customer/item minimums
    into their parent nodes."""
    notes: list[str] = []
    min_leaf = _min_leaf(len(y), tc)
    alpha = _select_ccp_alpha(x, y, min_leaf, tc.max_depth, seed)
    model = DecisionTreeRegressor(
        max_depth=tc.max_depth, min_samples_leaf=min_leaf, random_state=seed, ccp_alpha=alpha
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
        out.append(
            {
                "leaf_id": int(leaf_id),
                "rule": " AND ".join(conds) if conds else "(all lines)",
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


def _enforce_minimums_nearest(lines: pl.DataFrame, cell_col: str, tc: TreeConfig) -> pl.DataFrame:
    """Merge cells violating line/customer/item minimums into the cell with the
    nearest median price index. Adds `_cell_valid`."""
    assigned = lines[cell_col].to_numpy().astype(np.int64).copy()
    min_lines = _min_leaf(lines.height, tc)
    cust = lines["customer_id"].to_numpy()
    item = lines["item_id"].to_numpy()
    pidx = lines["price_index"].to_numpy()
    for _ in range(64):
        frame = pl.DataFrame({"cell": assigned, "c": cust, "i": item, "p": pidx})
        stats = frame.group_by("cell").agg(
            pl.len().alias("n"),
            pl.col("c").n_unique().alias("nc"),
            pl.col("i").n_unique().alias("ni"),
            pl.col("p").median().alias("p50"),
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
        # Merge the single worst (smallest) violating cell per iteration.
        worst = bad.sort("n").to_dicts()[0]
        others = stats.filter(pl.col("cell") != worst["cell"])
        target = (
            others.with_columns((pl.col("p50") - worst["p50"]).abs().alias("d"))
            .sort("d")
            .to_dicts()[0]["cell"]
        )
        assigned[assigned == worst["cell"]] = target
    return lines.with_columns(pl.Series("_cell_valid", assigned))


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
        clauses = [rule_str(paths[leaf]) for leaf in leaf_list if leaf in paths]
        rule = " OR ".join(f"({c})" for c in clauses[:6])
        if len(clauses) > 6:
            rule += f" OR ... ({len(clauses) - 6} more)"
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
    pairs = work.group_by(pair_keys).agg(
        pl.col("price_index").median().alias("price_index"),
        pl.len().alias("n_lines"),
        *[pl.col(f).mode().first().alias(f) for f in CONTEXT_FEATURES],
    )
    _leakage_guard(CONTEXT_FEATURES, rules)
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

    def fit_partition(
        fit_idx: np.ndarray,
    ) -> tuple[DecisionTreeRegressor, TargetEncoder, dict[int, int], np.ndarray, pl.DataFrame]:
        """Grow tree on the given pairs, consolidate commercially
        indistinguishable leaves, enforce minimums on lines."""
        fit_pairs = pairs[fit_idx.tolist()]
        enc = TargetEncoder(names).fit(fit_pairs, "price_index")
        m = DecisionTreeRegressor(max_depth=tc.max_depth, min_samples_leaf=pair_min, random_state=0)
        m.fit(enc.transform(fit_pairs), fit_pairs["price_index"].to_numpy())
        # Consolidation means computed on the fitting pairs only.
        fit_leaves = m.apply(enc.transform(fit_pairs))
        cell_of = _consolidate_by_mean(fit_leaves, fit_pairs["price_index"].to_numpy(), threshold)
        all_leaves = m.apply(enc.transform(pairs))
        cells = np.array([cell_of.get(int(leaf), int(leaf)) for leaf in all_leaves], dtype=np.int64)
        lines = work.join(
            pairs.select(*pair_keys).with_columns(pl.Series("_cell", cells)),
            on=pair_keys,
            how="left",
        )
        lines = _enforce_minimums_nearest(lines, "_cell", tc)
        return m, enc, cell_of, lines["_cell_valid"].to_numpy(), lines

    train_idx = np.flatnonzero(~test_mask)
    model, encoder, cell_of_full, full_leaves, lines_full = fit_partition(train_idx)
    notes = list(notes_pre)
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

    # Bootstrap stability: resample pairs, refit, compare line partitions.
    n_boot = rules.segmentation.bootstrap_resamples
    p50_full = lines_full.group_by("_cell_valid").agg(pl.col("price_index").median().alias("p50"))
    target_full = lines_full.join(p50_full, on="_cell_valid", how="left")["p50"].to_numpy()
    aris: list[float] = []
    changed_shares: list[float] = []
    for _b in range(n_boot):
        idx = rng.integers(0, n, n)
        _, _, _, bleaves, blines = fit_partition(idx)
        aris.append(float(adjusted_rand_score(full_leaves, bleaves)))
        bp50 = blines.group_by("_cell_valid").agg(pl.col("price_index").median().alias("p50"))
        btarget = blines.join(bp50, on="_cell_valid", how="left")["p50"].to_numpy()
        changed_shares.append(float(np.mean(np.abs(btarget - target_full) > 0.02)))

    assignments = lines_full.select("source_line_key").with_columns(
        pl.Series("segment_leaf", full_leaves.astype(np.int64))
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
        notes=notes,
    )


def fit_margin_driver_tree(df: pl.DataFrame, rules: Rules, *, seed: int = 0) -> TreeResult:
    """Margin-driver tree: pocket margin %, context + discount/cost features.
    Output is explanatory, never a pricing segmentation."""
    tc = rules.segmentation.margin_driver_tree
    work = df.filter(
        (pl.col("pocket_revenue_cents") > 0)
        & pl.col("pocket_gp_cents").is_not_null()
        & pl.col("gross_list_value_cents").is_not_null()
    ).with_columns(
        (pl.col("pocket_gp_cents") / pl.col("pocket_revenue_cents")).alias("pocket_margin_pct"),
        (pl.col("discount_cents") / pl.col("gross_list_value_cents").clip(lower_bound=1)).alias(
            "_disc_pct"
        ),
        (pl.col("rebate_actual_cents") / pl.col("net_amount_cents").clip(lower_bound=1)).alias(
            "_reb_rate"
        ),
        (pl.col("product_cost_cents") / pl.col("net_amount_cents").clip(lower_bound=1)).alias(
            "_cost_ratio"
        ),
    )
    work = work.filter(pl.col("pocket_margin_pct").is_finite())

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
