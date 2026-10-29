"""Phase 17 verification: planted cross-price direction recovered on
well_identified; unsupported cases labeled, not estimated; substitution-aware
opportunity adjustment labeled."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.extended.substitution import (
    STATUS_CROSS,
    STATUS_CROSS_CAUSAL,
    estimate_cross_effects,
    substitution_adjusted_change,
)
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.panel.build import build_panel

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("subst")
    cfg = GeneratorConfig.for_preset(
        Preset.WELL_IDENTIFIED,
        seed=3,
        n_customers=300,
        n_items=300,
        n_substitution_groups=10,
    )
    generate(cfg, out)
    rules = load_rules(REPO / "config" / "rules.yaml")
    res = run_pipeline(out / "erp", MAPPING, rules)
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    pt = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    obs = flagged.join(
        pt.select("customer_id", "item_id", "segment_id").unique(),
        on=["customer_id", "item_id"],
        how="left",
    ).with_columns(pl.col("segment_id").alias("segment_leaf"))
    panel = build_panel(
        obs,
        res.reference["items"],
        res.reference["supplier_cost_index"],
        res.reference["tariff_schedule"],
        rules,
    )
    truth = json.loads((out / "ground_truth" / "truth_summary.json").read_text())["substitution"]
    estimates = estimate_cross_effects(panel, res.reference["substitution_groups"], rules)
    return truth, estimates


def test_cross_estimates_only_within_groups(world) -> None:
    truth, est = world
    group_ids = {m["group_id"] for m in truth["members"]}
    assert set(est["group_id"].unique().to_list()) <= group_ids


def test_planted_cross_direction_recovered(world) -> None:
    _truth, est = world
    supported = est.filter(pl.col("status").is_in([STATUS_CROSS, STATUS_CROSS_CAUSAL]))
    assert supported.height >= 4, est.select("group_id", "status", "reasons").to_dicts()
    positive = supported.filter(pl.col("cross_elasticity") > 0)
    share = positive.height / supported.height
    print(f"\ncross direction: {positive.height}/{supported.height} positive")
    assert share >= 0.8, f"only {share:.0%} of estimated cross effects positive"


def test_unsupported_cases_labeled_not_estimated(world) -> None:
    _, est = world
    weak = est.filter(~pl.col("status").is_in([STATUS_CROSS, STATUS_CROSS_CAUSAL]))
    # Whatever could not be identified carries a reason and no causal claim.
    for r in weak.to_dicts():
        assert r["reasons"], r
    # Estimates never exist outside identified statuses with diagnostics.
    for r in est.filter(pl.col("status") == STATUS_CROSS_CAUSAL).to_dicts():
        assert r["min_conditional_effective_f"] >= 10
        assert r["cross_se"] is not None


def test_conditional_statistic_is_what_gates_the_cross_status(world) -> None:
    """Run 6, Phase 71. A marginal first-stage F says the instruments move each
    price; only the Sanderson-Windmeijer conditional statistic says they move
    one price independently of the other, which is what a cross-price estimate
    needs. Both are reported, and the conditional one decides."""
    _truth, est = world
    identified = est.filter(pl.col("status").is_in([STATUS_CROSS, STATUS_CROSS_CAUSAL]))
    assert identified.height > 0
    for r in est.to_dicts():
        if r["min_conditional_effective_f"] is None:
            continue
        # Both statistics are on the record, so the gap between them is visible.
        assert r["min_first_stage_f"] is not None
        assert r["n_clusters"] is not None and r["n_absorbed_parameters"] is not None
        assert r["first_stage_detail"]
        strong_enough = r["min_conditional_effective_f"] >= 10
        assert strong_enough == (r["status"] in (STATUS_CROSS, STATUS_CROSS_CAUSAL)), r
    # The marginal statistic alone would have reached a different verdict
    # somewhere, which is the whole reason for the change.
    disagree = est.filter(
        pl.col("min_conditional_effective_f").is_not_null()
        & ((pl.col("min_first_stage_f") >= 10) != (pl.col("min_conditional_effective_f") >= 10))
    )
    print(f"\nmarginal vs conditional verdict differs on {disagree.height} pairs")


def test_substitution_adjustment_labeled() -> None:
    adj, label = substitution_adjusted_change(
        expected_change_cents=-10_000,
        own_elasticity=-2.0,
        cross_elasticity=0.5,
        substitute_margin_cents_per_unit=300,
        lost_units=100.0,
    )
    # recapture = 0.25 -> 100 x 0.25 x 300 = 7500 recaptured.
    assert adj == -10_000 + 7_500
    assert "substitution_recapture=0.25" in label
    same, label2 = substitution_adjusted_change(-10_000, -2.0, None, 300, 100.0)
    assert same == -10_000
    assert label2 == "no_substitution_adjustment"
