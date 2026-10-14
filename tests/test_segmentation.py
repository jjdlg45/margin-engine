"""Phase 4 verification: segment recovery (ARI), bootstrap stability,
leakage guard, leaf constraints, margin-driver separation.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.segment.trees import (
    TreeResult,
    fit_commercial_segments,
    fit_margin_driver_tree,
    prepare_segmentation_frame,
)

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


def _rules_fast() -> Rules:
    # Fewer bootstrap resamples for test speed; thresholds unchanged.
    return Rules.model_validate({"segmentation": {"bootstrap_resamples": 8}})


@pytest.fixture(scope="module")
def seg_world(tmp_path_factory: pytest.TempPathFactory) -> tuple[pl.DataFrame, TreeResult]:
    out = tmp_path_factory.mktemp("seg")
    # Default generator scale: section 8 targets apply at default settings.
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=3)
    generate(cfg, out)
    rules = _rules_fast()
    res = run_pipeline(out / "erp", MAPPING, rules)
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    df = prepare_segmentation_frame(flagged, res.reference["customers"], res.reference["items"])
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    planted = panel.select("customer_id", "item_id", "segment_id").unique()
    df = df.join(planted, on=["customer_id", "item_id"], how="left")
    result = fit_commercial_segments(df, rules, planted_segment_col="segment_id", seed=1)
    return df, result


def test_out_of_sample_ari_vs_planted(seg_world: tuple[pl.DataFrame, TreeResult]) -> None:
    _, result = seg_world
    assert result.out_of_sample_ari_vs_planted is not None
    assert result.out_of_sample_ari_vs_planted >= 0.70, (
        f"ARI {result.out_of_sample_ari_vs_planted:.3f} < 0.70"
    )


def test_bootstrap_stability(seg_world: tuple[pl.DataFrame, TreeResult]) -> None:
    _, result = seg_world
    assert result.bootstrap_median_ari is not None
    assert result.bootstrap_median_ari >= 0.80, (
        f"bootstrap median ARI {result.bootstrap_median_ari:.3f} < 0.80"
    )
    # Recommendation-change share is reported (not a hard target).
    assert result.recommendation_change_share is not None
    assert 0 <= result.recommendation_change_share <= 1


def test_leaf_constraints_satisfied(seg_world: tuple[pl.DataFrame, TreeResult]) -> None:
    df, result = seg_world
    rules = _rules_fast()
    tc = rules.segmentation.commercial_tree
    joined = df.join(result.leaf_assignments, on="source_line_key", how="inner")
    per_leaf = joined.group_by("segment_leaf").agg(
        pl.len().alias("n"),
        pl.col("customer_id").n_unique().alias("nc"),
        pl.col("item_id").n_unique().alias("ni"),
    )
    assert (per_leaf["nc"] >= tc.min_leaf_customers).all()
    assert (per_leaf["ni"] >= tc.min_leaf_items).all()
    assert (per_leaf["n"] >= tc.min_leaf_lines).all()


def test_segment_rules_readable(seg_world: tuple[pl.DataFrame, TreeResult]) -> None:
    _, result = seg_world
    assert len(result.rules) >= 2
    for r in result.rules:
        assert r["rule"]
        assert r["n_lines"] > 0


def test_leakage_guard_blocks_price_features() -> None:
    rules = Rules()
    df = pl.DataFrame(
        {
            "source_line_key": ["a", "b"],
            "customer_id": ["c", "c"],
            "item_id": ["i", "i"],
            "price_index": [0.9, 0.8],
            # A poisoned context column whose name carries a forbidden term.
            "industry": ["x", "y"],
        }
    )
    import margin_engine.segment.trees as trees

    original = trees.CONTEXT_FEATURES
    trees.CONTEXT_FEATURES = [*original, "avg_discount_given"]
    try:
        df = df.with_columns(pl.lit("hi").alias("avg_discount_given"))
        for col in original:
            if col not in df.columns:
                df = df.with_columns(pl.lit("u").alias(col))
        with pytest.raises(ValueError, match="leakage guard"):
            fit_commercial_segments(df, rules)
    finally:
        trees.CONTEXT_FEATURES = original


def test_commercial_features_exclude_price_columns(
    seg_world: tuple[pl.DataFrame, TreeResult],
) -> None:
    _, result = seg_world
    forbidden = ("price", "discount", "cost", "rebate", "margin")
    for f in result.feature_names:
        assert not any(t in f.lower() for t in forbidden), f


def test_margin_driver_tree_separate(seg_world: tuple[pl.DataFrame, TreeResult]) -> None:
    df, _ = seg_world
    rules = _rules_fast()
    result = fit_margin_driver_tree(df, rules, seed=1)
    assert result.kind == "margin_drivers"
    assert result.target == "pocket_margin_pct"
    # May legitimately use discount/cost bands.
    assert any("discount_pct_band" in f or "cost_ratio_band" in f for f in result.feature_names)
    # Labeled explanatory, never price-setting segments.
    assert any("explanatory" in n for n in result.notes)
    per_leaf = len(result.rules)
    assert per_leaf >= 1
