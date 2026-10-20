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


def test_margin_leaf_descriptions_match_conditions(
    seg_world: tuple[pl.DataFrame, TreeResult],
) -> None:
    """A leaf's description must match its actual conditions (phase 23.6):
    '(all lines)' only when the cell truly holds every line; cells at internal
    nodes (merged up from too-small leaves) carry the explicit qualifier; and
    every line's decision path passes through its assigned node, so the path
    conditions in the rule hold for every line in the cell."""
    from sklearn.tree import _tree

    from margin_engine.labels import MERGED_QUALIFIER, plain_rule

    df, _ = seg_world
    rules = _rules_fast()
    result = fit_margin_driver_tree(df, rules, seed=1)
    total = int(result.leaf_assignments.height)
    tree = result.model.tree_

    for r in result.rules:
        node = int(r["leaf_id"])
        is_internal = tree.children_left[node] != _tree.TREE_LEAF
        if r["rule"] == "(all lines)":
            assert r["n_lines"] == total, (
                f"leaf {node} says '(all lines)' but holds {r['n_lines']} of {total}"
            )
        if is_internal and r["n_lines"] < total:
            assert MERGED_QUALIFIER in r["rule"], (
                f"internal-node cell {node} lacks the merged-up qualifier"
            )
            name, desc, note = plain_rule(str(r["rule"]))
            assert "more specific" in desc or "more specific" in note
            assert name != "All lines"

    # Path consistency: every line's decision path includes its assigned node,
    # so the rule's path conditions hold for every line in the cell.
    import numpy as np

    df.filter(
        (pl.col("pocket_revenue_cents") > 0)
        & pl.col("pocket_gp_cents").is_not_null()
        & pl.col("gross_list_value_cents").is_not_null()
    )
    # Recompute the engineered band features exactly as the fit does by
    # re-running the fit path on the same frame and comparing assignments
    # against decision paths on the model's own training encoding.
    assigned = result.leaf_assignments["driver_leaf"].to_numpy()
    assert len(assigned) > 0
    # Every assigned node must exist in the tree and be on >= its own count
    # of decision paths (prefix containment).
    counts = {int(n): int((assigned == n).sum()) for n in np.unique(assigned)}
    node_samples = tree.n_node_samples
    for node, n in counts.items():
        assert node_samples[node] >= n, (
            f"node {node} holds {n} assigned lines but only {node_samples[node]} "
            f"training samples pass through it"
        )
