"""Run 6, Phase 76 (Finding 11): segmentation features that carry the answer.

Three separate leaks were in the shipped tree, and each is closed by a
different mechanism:

1. `revenue_abc` was cut on the same period's revenue - price x quantity -
   so the tree could "explain" the price index with a coarse restatement of
   it. The ABC the tree sees is now cut on a window that ENDS before the
   line's own period.
2. The order-size band was cut on order net value, which is also price x
   quantity. It is now cut on quantity.
3. The target encoding was fit on the rows it then encoded, so a thin
   category was handed the mean of the targets the split was about to be
   scored against. It is now fit out of fold.

The guard that keeps new features honest reads declared lineage, not names.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from margin_engine.config import FeatureLineage, Rules
from margin_engine.dispersion.analytics import UNRATED, lagged_abc_classification
from margin_engine.segment import trees
from margin_engine.segment.trees import (
    TargetEncoder,
    _leakage_guard,
    _order_size_band,
    fit_commercial_segments,
    time_based_holdout,
)


def _rules(**segmentation: object) -> Rules:
    base: dict[str, object] = {"bootstrap_resamples": 2}
    base.update(segmentation)
    return Rules.model_validate({"segmentation": base})


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------


def _with_lineage(name: str, **kw: object) -> Rules:
    rules = _rules()
    lineage = dict(rules.segmentation.feature_lineage)
    lineage[name] = FeatureLineage(
        source_columns=("whatever",),
        formula="f",
        reference_window="w",
        uses_outcome_period_data=bool(kw.get("uses_outcome_period_data", False)),
        embeds_realized_price=bool(kw.get("embeds_realized_price", False)),
    )
    seg = rules.segmentation.model_copy(update={"feature_lineage": lineage})
    return rules.model_copy(update={"segmentation": seg})


def test_an_innocent_name_does_not_get_a_feature_past_the_guard() -> None:
    """The whole point of Finding 11: `revenue_abc` passed a name check."""
    rules = _with_lineage("buyer_tier", embeds_realized_price=True)
    with pytest.raises(ValueError, match="embed the realized price"):
        _leakage_guard(["industry", "buyer_tier"], rules)


def test_an_undeclared_feature_is_refused_rather_than_assumed_innocent() -> None:
    with pytest.raises(ValueError, match="no feature_lineage entry"):
        _leakage_guard(["industry", "mystery_column"], _rules())


def test_the_name_check_still_fires() -> None:
    rules = _with_lineage("avg_discount_given")
    with pytest.raises(ValueError, match="forbidden terms"):
        _leakage_guard(["industry", "avg_discount_given"], rules)


def test_same_period_but_price_free_features_are_reported_not_refused() -> None:
    same_period = _leakage_guard(["industry", "order_size_band", "abc_class"], _rules())
    assert same_period == ["order_size_band"]


def test_the_shipped_feature_set_passes_its_own_guard() -> None:
    same_period = _leakage_guard(list(trees.CONTEXT_FEATURES), _rules())
    assert "abc_class" not in same_period


# ---------------------------------------------------------------------------
# the lagged ABC
# ---------------------------------------------------------------------------


def _abc_world() -> pl.DataFrame:
    """One item that is tiny for six months then enormous, one steady item."""
    rows = []
    key = 0
    for month in range(1, 13):
        big = 1 if month <= 6 else 400
        for item, qty in (("TINY", big), ("STEADY", 50)):
            key += 1
            rows.append(
                {
                    "source_line_key": f"L{key}",
                    "invoice_date": dt.date(2024, month, 15),
                    "item_id": item,
                    "order_id": f"O{key}",
                    "quantity_norm": float(qty),
                    "net_amount_cents": qty * 100,
                }
            )
    return pl.DataFrame(rows)


def test_a_lines_class_never_uses_its_own_period() -> None:
    """TINY only becomes large in month 7; its class in month 7 must still
    reflect the six months in which it was not."""
    out = lagged_abc_classification(_abc_world(), _rules(abc_feature_lag_periods=6))
    row = out.filter((pl.col("item_id") == "TINY") & (pl.col("_abc_period") == 2024 * 12 + 7))
    assert row.height == 1
    assert row["abc_class"][0] == "C", "month 7 was classed on month 7's own revenue"


def test_an_item_with_no_history_in_the_window_is_unrated_not_c() -> None:
    out = lagged_abc_classification(_abc_world(), _rules(abc_feature_lag_periods=6))
    first = out.filter(pl.col("_abc_period") == 2024 * 12 + 1)
    assert set(first["abc_class"].to_list()) == {UNRATED}


def test_the_basis_changes_the_answer_when_revenue_and_units_diverge() -> None:
    """Ten items ranked one way by revenue and the exact opposite by units.

    The item that is cheapest per unit and sells the most of them is a C on
    revenue and an A on units; the jewel is the other way round. Which basis
    the segmentation feature uses is therefore a real choice, not a label.
    """
    rows = []
    for month in (1, 2):
        for i in range(10):
            # Revenue descends with i; quantity ascends with i.
            cents = (10 - i) * 1_000
            qty = float(i + 1) * 100.0
            rows.append(
                {
                    "source_line_key": f"I{i}-{month}",
                    "invoice_date": dt.date(2024, month, 10),
                    "item_id": f"I{i}",
                    "order_id": f"O{i}-{month}",
                    "quantity_norm": qty,
                    "net_amount_cents": cents,
                }
            )
    df = pl.DataFrame(rows)
    by_revenue = lagged_abc_classification(df, _rules(abc_feature_basis="revenue"))
    by_units = lagged_abc_classification(df, _rules(abc_feature_basis="units"))
    second = 2024 * 12 + 2

    def cls(out: pl.DataFrame, item: str) -> str:
        row = out.filter((pl.col("item_id") == item) & (pl.col("_abc_period") == second))
        return str(row["abc_class"][0])

    order = {"A": 0, "B": 1, "C": 2, UNRATED: 3}
    # The top revenue item outranks the top unit item on revenue, and the
    # ranking flips when the basis does.
    assert order[cls(by_revenue, "I0")] < order[cls(by_revenue, "I9")]
    assert order[cls(by_units, "I9")] < order[cls(by_units, "I0")]
    assert cls(by_revenue, "I9") != cls(by_units, "I9")


def test_a_window_with_nothing_in_it_rates_nobody() -> None:
    df = _abc_world().with_columns(
        pl.lit(0).alias("net_amount_cents"), pl.lit(0.0).alias("quantity_norm")
    )
    out = lagged_abc_classification(df, _rules())
    assert set(out["abc_class"].unique().to_list()) == {UNRATED}


def test_the_lagged_classification_is_deterministic() -> None:
    a = lagged_abc_classification(_abc_world(), _rules())
    b = lagged_abc_classification(_abc_world(), _rules())
    assert a.equals(b)


# ---------------------------------------------------------------------------
# order size in units
# ---------------------------------------------------------------------------


def test_order_size_bands_ignore_what_the_order_was_worth() -> None:
    """Two orders of the same size land in the same band however differently
    they were priced - the band used to be cut on net value."""
    rows = []
    for i in range(30):
        qty = 1.0 + i
        # Price per unit swings wildly and must not move the band.
        cents = int(qty * (10_000 if i % 2 else 10))
        rows.append(
            {
                "source_line_key": f"L{i}",
                "order_id": f"O{i}",
                "quantity_norm": qty,
                "net_amount_cents": cents,
            }
        )
    out = _order_size_band(pl.DataFrame(rows))
    bands = dict(zip(out["quantity_norm"].to_list(), out["order_size_band"].to_list(), strict=True))
    assert bands[2.0] == bands[3.0] == "small"
    assert bands[29.0] == bands[30.0] == "large"


# ---------------------------------------------------------------------------
# out-of-fold target encoding
# ---------------------------------------------------------------------------


def _noise_frame(n: int = 400, seed: int = 0) -> pl.DataFrame:
    """A high-cardinality category that is pure noise: each level appears
    twice and carries no information about the target at all."""
    rng = np.random.default_rng(seed)
    return pl.DataFrame(
        {
            "junk": [f"c{i // 2}" for i in range(n)],
            "y": rng.normal(size=n),
        }
    )


def test_an_in_sample_encoding_hands_the_tree_its_own_target() -> None:
    df = _noise_frame()
    enc = TargetEncoder(["junk"], shrinkage_k=0.0)
    in_sample = enc.fit(df, "y").transform(df)[:, 0]
    oof = TargetEncoder(["junk"], shrinkage_k=0.0).fit_cross_fitted(df, "y", n_folds=5, seed=0)[
        :, 0
    ]
    y = df["y"].to_numpy()
    # The in-sample encoding of a two-row category is half that row's own
    # target, so it correlates strongly with noise it cannot know.
    assert abs(float(np.corrcoef(in_sample, y)[0, 1])) > 0.6
    assert abs(float(np.corrcoef(oof, y)[0, 1])) < 0.2


def _refit_per_fold(
    df: pl.DataFrame, target: str, features: list[str], k: float, n_folds: int, seed: int
) -> np.ndarray:
    """The obvious implementation: refit the encoder once per fold."""
    rng = np.random.default_rng(seed)
    fold = rng.permutation(df.height) % n_folds
    out = np.empty((df.height, len(features)), dtype=np.float64)
    for f in range(n_folds):
        held, rest = fold == f, fold != f
        part = TargetEncoder(features, k).fit(df.filter(pl.Series(rest)), target)
        out[held] = part.transform(df.filter(pl.Series(held)))
    return out


@pytest.mark.parametrize("k", [0.0, 20.0])
def test_the_fast_cross_fitting_equals_refitting_every_fold(k: float) -> None:
    """The encoder computes each fold by subtracting that fold's totals from
    the whole, instead of refitting on the other folds. That is only allowed
    if it gives the same numbers - including for a category that appears in
    one fold alone, where a refit falls back to the other folds' mean.
    """
    rng = np.random.default_rng(11)
    n = 300
    df = pl.DataFrame(
        {
            # "solo" levels appear once, so some land in exactly one fold.
            "cat": [f"c{i % 7}" if i % 5 else f"solo{i}" for i in range(n)],
            "other": [f"d{i % 3}" for i in range(n)],
            "y": rng.normal(size=n),
        }
    )
    fast = TargetEncoder(["cat", "other"], k).fit_cross_fitted(df, "y", n_folds=5, seed=4)
    slow = _refit_per_fold(df, "y", ["cat", "other"], k, 5, 4)
    assert np.allclose(fast, slow, rtol=0, atol=1e-12)


def test_cross_fitting_leaves_the_prediction_time_maps_full_sample() -> None:
    df = _noise_frame(n=100)
    enc = TargetEncoder(["junk"])
    enc.fit_cross_fitted(df, "y", n_folds=5, seed=0)
    full = TargetEncoder(["junk"]).fit(df, "y")
    assert enc.maps == full.maps


def test_cross_fitting_is_reproducible_and_falls_back_on_tiny_frames() -> None:
    df = _noise_frame(n=100)
    a = TargetEncoder(["junk"]).fit_cross_fitted(df, "y", n_folds=5, seed=7)
    b = TargetEncoder(["junk"]).fit_cross_fitted(df, "y", n_folds=5, seed=7)
    assert np.array_equal(a, b)
    tiny = _noise_frame(n=6)
    enc = TargetEncoder(["junk"])
    assert np.array_equal(enc.fit_cross_fitted(tiny, "y", n_folds=5, seed=0), enc.transform(tiny))


# ---------------------------------------------------------------------------
# a planted world where the shortcut and the truth disagree
# ---------------------------------------------------------------------------


def _shortcut_world(seed: int = 0) -> pl.DataFrame:
    """True segments are industries. A second attribute predicts the price
    index just as well but cuts across the industries, so a tree that takes
    the shortcut gets a partition that is wrong about the truth.

    This is the shape of the real leak: contemporaneous revenue correlates
    with the price index for reasons that have nothing to do with commercial
    structure (big orders get keener prices), and a tree handed it will carve
    the market by order size instead of by who the customer is.
    """
    rng = np.random.default_rng(seed)
    rows = []
    key = 0
    # 120 customers, not 40: industry x shortcut is four cells, and at 40
    # customers each cell holds 10 - below the shipped 20-customer leaf
    # minimum, so whether the shortcut cells survive the minimum-enforcing
    # merge is a coin flip and the test is flaky. At 120 each cell holds 30
    # and the tree is free to take the shortcut if it wants to, which is the
    # only way this test can mean anything.
    for cust in range(120):
        industry = "alpha" if cust < 60 else "beta"
        base = 0.90 if industry == "alpha" else 0.70
        # The shortcut splits customers a different way than industry does.
        shortcut = "heavy" if cust % 2 == 0 else "light"
        offset = 0.08 if shortcut == "heavy" else -0.08
        for item in range(20):
            for month in range(1, 13):
                key += 1
                rows.append(
                    {
                        "source_line_key": f"L{key}",
                        "invoice_date": dt.date(2024, month, 10),
                        "customer_id": f"C{cust:03d}",
                        "item_id": f"I{item:03d}",
                        "price_index": base + offset + float(rng.normal(0, 0.01)),
                        "industry": industry,
                        "size_band": "mid",
                        "region": "north",
                        "channel": "direct",
                        "contract_type": "none",
                        "item_family": f"F{item % 4}",
                        "item_role": "standard",
                        "order_size_band": "medium",
                        "abc_class": "A",
                        "shortcut_class": shortcut,
                        "segment_id": industry,
                    }
                )
    return pl.DataFrame(rows)


def _fit_with_features(df: pl.DataFrame, features: list[str], rules: Rules, seed: int = 1) -> float:
    original = trees.CONTEXT_FEATURES
    trees.CONTEXT_FEATURES = features
    try:
        res = fit_commercial_segments(df, rules, planted_segment_col="segment_id", seed=seed)
    finally:
        trees.CONTEXT_FEATURES = original
    assert res.out_of_sample_ari_vs_planted is not None
    return res.out_of_sample_ari_vs_planted


def test_the_tree_recovers_the_truth_when_the_shortcut_is_kept_out() -> None:
    ari = _fit_with_features(_shortcut_world(), ["industry", "item_family", "item_role"], _rules())
    assert ari >= 0.70, f"ARI {ari:.3f}"


def test_the_guard_refuses_the_shortcut_when_its_lineage_is_declared() -> None:
    """Declared honestly, the feature never reaches the tree."""
    rules = _with_lineage("shortcut_class", embeds_realized_price=True)
    with pytest.raises(ValueError, match="embed the realized price"):
        _fit_with_features(_shortcut_world(), ["industry", "shortcut_class"], rules)


def test_the_shortcut_really_does_destroy_the_partition() -> None:
    """The declaration is load-bearing, not decorative: declare the same
    feature as price-free and the tree takes it, and the recovered partition
    stops matching the planted one. This is what the guard prevents.
    """
    df = _shortcut_world()
    rules = _with_lineage("shortcut_class", embeds_realized_price=False)
    with_shortcut = _fit_with_features(df, ["industry", "shortcut_class"], rules)
    without = _fit_with_features(df, ["industry"], rules)
    assert without >= 0.70
    assert with_shortcut < without - 0.2, (
        f"shortcut {with_shortcut:.3f} vs clean {without:.3f}: the planted world "
        "does not actually punish the shortcut, so the test proves nothing"
    )


def test_a_contemporaneous_abc_would_have_been_a_shortcut() -> None:
    """The specific instance of Finding 11: an ABC cut on the same period's
    revenue tracks the price index, and the lagged class cannot, because in
    the first period it has nothing to look back at."""
    rows = _shortcut_world().with_columns(
        (pl.col("price_index") * 1000).cast(pl.Int64).alias("net_amount_cents"),
        pl.lit(1.0).alias("quantity_norm"),
        pl.col("source_line_key").alias("order_id"),
    )
    lagged = lagged_abc_classification(rows, _rules(abc_feature_lag_periods=3))
    first = lagged.filter(pl.col("_abc_period") == 2024 * 12 + 1)
    assert set(first["abc_class"].to_list()) == {UNRATED}


# ---------------------------------------------------------------------------
# the time-based holdout
# ---------------------------------------------------------------------------

_FEATURES = ["industry", "item_family", "item_role", "order_size_band", "abc_class"]


def test_real_structure_beats_a_single_median_on_later_periods() -> None:
    h = time_based_holdout(_shortcut_world(), _FEATURES, _rules(), seed=1)
    assert h is not None
    assert h.n_eval_periods > 0 and h.n_eval_pairs > 0
    assert h.mae is not None and h.mae_baseline is not None
    assert h.mae < h.mae_baseline
    assert "better than a single median" in h.note


def test_structureless_data_does_not_pretend_to_generalize() -> None:
    """Price index is noise: the segmentation must not beat one median by
    anything worth reporting."""
    base = _shortcut_world()
    rng = np.random.default_rng(3)
    df = base.with_columns(pl.Series("price_index", rng.normal(0.8, 0.05, base.height)))
    h = time_based_holdout(df, _FEATURES, _rules(), seed=1)
    assert h is not None and h.mae is not None and h.mae_baseline is not None
    assert h.mae >= h.mae_baseline * 0.98


def test_too_little_history_says_so_instead_of_scoring() -> None:
    df = _shortcut_world().filter(pl.col("invoice_date") < dt.date(2024, 3, 1))
    h = time_based_holdout(df, _FEATURES, _rules(), seed=1)
    assert h is not None
    assert h.mae is None
    assert "too short" in h.note


def test_the_same_period_features_get_their_own_number() -> None:
    h = time_based_holdout(_shortcut_world(), _FEATURES, _rules(), seed=1)
    assert h is not None
    assert h.same_period_features == ["order_size_band"]
    assert h.mae_without_same_period_features is not None


def test_the_fitted_result_carries_the_holdout_and_says_what_it_found() -> None:
    res = fit_commercial_segments(
        _shortcut_world(), _rules(), planted_segment_col="segment_id", seed=1
    )
    assert res.holdout is not None
    assert any("single median" in n for n in res.notes)
    assert any("same period as the price index" in n for n in res.notes)
    assert any("context feature order size band is" in n for n in res.notes)
