"""Phase 10 verification: hierarchy levels, national vs local separation,
price protection end to end."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.extended.hierarchy import apply_analysis_level, hierarchy_map
from margin_engine.guidance.bands import build_guidance
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline
from margin_engine.opportunity.sizing import price_opportunities
from margin_engine.panel.build import build_panel
from margin_engine.sensitivity.estimate import run_sensitivity

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PipelineResult]:
    out = tmp_path_factory.mktemp("hier")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=9, n_customers=200, n_items=250)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, load_rules(REPO / "config" / "rules.yaml"))
    return out, res


def test_hierarchy_map_falls_back_to_self(world: tuple[Path, PipelineResult]) -> None:
    _, res = world
    hmap = hierarchy_map(res.reference["customers"])
    assert hmap is not None
    # Every ship-to has a bill-to and a parent (itself when unassigned).
    assert hmap["bill_to_id"].null_count() == 0
    assert hmap["parent_id"].null_count() == 0
    # The generator plants shared bill-tos: strictly fewer bill-tos than ship-tos.
    assert hmap["bill_to_id"].n_unique() < hmap["ship_to_id"].n_unique()


def test_analysis_level_bill_to_groups(world: tuple[Path, PipelineResult]) -> None:
    _, res = world
    rules = Rules.model_validate({"hierarchy": {"analysis_level": "bill_to"}})
    out, notes = apply_analysis_level(res.observations, res.reference["customers"], rules)
    assert any("bill_to" in n for n in notes)
    assert "ship_to_customer_id" in out.columns
    # Row count unchanged; distinct analysis customers shrink.
    assert out.height == res.observations.height
    assert out["customer_id"].n_unique() < res.observations["customer_id"].n_unique()
    # Dollars unchanged: grouping never moves money.
    assert out["net_amount_cents"].sum() == res.observations["net_amount_cents"].sum()


def test_ship_to_level_is_identity(world: tuple[Path, PipelineResult]) -> None:
    _, res = world
    out, notes = apply_analysis_level(res.observations, res.reference["customers"], Rules())
    assert notes == []
    assert out.height == res.observations.height
    assert "ship_to_customer_id" not in out.columns


def test_national_vs_local_separated_as_planted(world: tuple[Path, PipelineResult]) -> None:
    """National-agreement (price-protected contract) lines price uniformly
    within the agreement; local open-market lines disperse. The planted
    structure must be visible at the parent analysis level."""
    _, res = world
    customers = res.reference["customers"]
    obs = res.observations.join(
        customers.select("customer_id", "contract_type"), on="customer_id", how="left"
    ).with_columns(
        (pl.col("net_amount_cents") / pl.col("quantity_norm") / pl.col("eff_list_cents")).alias(
            "pidx"
        )
    )
    national = obs.filter(
        (pl.col("contract_type") == "national") & pl.col("contract_id").is_not_null()
    )
    local_open = obs.filter(pl.col("contract_id").is_null())
    assert national.height > 50
    # Within each national customer x item, prices are flat (contract-fixed);
    # open-market pairs move with shocks and discounts.
    nat_spread = (
        national.group_by("customer_id", "item_id")
        .agg(pl.col("pidx").std().alias("sd"))
        .select(pl.col("sd").fill_null(0).median())
        .item()
    )
    loc_spread = (
        local_open.group_by("customer_id", "item_id")
        .agg(pl.col("pidx").std().alias("sd"))
        .select(pl.col("sd").fill_null(0).median())
        .item()
    )
    assert nat_spread < 0.002
    assert loc_spread > 0.01


def test_protected_lines_never_get_price_increase_recommendations(
    world: tuple[Path, PipelineResult],
) -> None:
    """End to end on a generated world: no opportunity record exists for any
    contract-protected customer-item pair."""
    _out, res = world
    rules = load_rules(REPO / "config" / "rules.yaml")
    from margin_engine.dispersion.analytics import run_dispersion
    from margin_engine.segment.trees import fit_commercial_segments, prepare_segmentation_frame

    fast = Rules.model_validate({"segmentation": {"bootstrap_resamples": 2}})
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], fast)
    df = prepare_segmentation_frame(flagged, res.reference["customers"], res.reference["items"])
    seg = fit_commercial_segments(df, fast, seed=1)
    df = df.join(seg.leaf_assignments, on="source_line_key", how="left")
    guidance = build_guidance(df, rules, res.reference["rebate_programs"])
    panel = build_panel(
        df,
        res.reference["items"],
        res.reference["supplier_cost_index"],
        res.reference["tariff_schedule"],
        rules,
    )
    sens = run_sensitivity(panel, rules)
    opps = price_opportunities(guidance.guidance, panel, sens, rules)
    protected_pairs = guidance.guidance.filter(pl.col("contract_protected")).select(
        "customer_id", "item_id"
    )
    assert protected_pairs.height > 0
    overlap = opps.join(protected_pairs, on=["customer_id", "item_id"], how="semi")
    assert overlap.height == 0
