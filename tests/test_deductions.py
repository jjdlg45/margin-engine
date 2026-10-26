"""Phase 66 (Run 6): deductions with explicit denominators — the exit gate.

Each fixture proves the REPORTED opportunity impact equals a RECOMPUTED
baseline-vs-scenario ledger: the candidate world is built with the price
actually raised and every contract formula re-applied (flat settlements at
rate x new net; tiered settlements at the retrospective rate x new basis), run
through the full pipeline, and the pocket-revenue delta compared to the
engine's opportunity figure to the cent.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from margin_engine.config import Rules
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.opportunity.deductions import (
    TieredDeductionContext,
    build_tiered_context,
    deduction_coverage,
)
from margin_engine.opportunity.sizing import price_opportunities
from tests.fixtures.builder import FixtureWorld

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


def _guidance(**kw: object) -> pl.DataFrame:
    base: dict[str, object] = {
        "customer_id": "C1",
        "item_id": "X1",
        "segment_leaf": 1,
        "guidance_status": "below_reference",
        "target_index": 0.95,
        "current_index": 0.90,
        "eff_list_cents": 10_000,
        "unit_cost_cents": 6_000,
        "invoice_floor_cents": 6_316,
        "band_p25": 0.92,
        "band_p75": 0.97,
        "contract_protected": False,
        "deduction_bp": 0,
        "fixed_deduction_cents": 0,
    }
    base.update(kw)
    return pl.DataFrame([base])


def _panel(qty: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "X1",
                "segment_leaf": 1,
                "month_idx": 24300,
                "qty": qty,
                "price_cf": 9000.0,
                "contract_id": None,
            }
        ]
    ).with_columns(pl.col("contract_id").cast(pl.String))


def _sensitivity(status: str = "descriptive_elasticity") -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "segment": 1,
                "iv": -2.0,
                "iv_ci_low": -2.2,
                "iv_ci_high": -1.8,
                "status": status,
                "support_log_lo": -0.5,
                "support_log_hi": 0.5,
            }
        ]
    )


def _world(
    tmp: Path,
    *,
    unit_price: str,
    net: str,
    settle: list[dict[str, str]],
    tiered: bool = False,
    flat_rate: str | None = None,
    tiered_settle: list[dict[str, str]] | None = None,
) -> FixtureWorld:
    w = FixtureWorld(tmp)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=10,
        UNIT_PRC=unit_price,
        EXT_NET=net,
        LIST_PRC="100.00",
        UNIT_CST="60.0000",
    )
    programs = []
    if flat_rate is not None:
        programs.append(
            {"PGM_ID": "RPS", "CUST_NO": "C1", "KIND": "settlement", "RATE_PCT": flat_rate}
        )
    if tiered:
        programs.append({"PGM_ID": "TIER01", "CUST_NO": "C1", "KIND": "tiered", "RATE_PCT": "0.0"})
        w.rebate_program_tiers = [
            {"PGM_ID": "TIER01", "MIN_AMT": "0.00", "RATE_PCT": "1.0"},
            {"PGM_ID": "TIER01", "MIN_AMT": "1000.00", "RATE_PCT": "2.0"},
        ]
    w.rebate_programs = programs
    w.rebate_settlements = settle
    if tiered_settle is not None:
        w.tiered_settlements = tiered_settle
    return w


def _pocket(tmp: Path, world: FixtureWorld) -> int:
    res = run_pipeline(world.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    return int(
        res.ledger.filter(pl.col("transaction_type") == "sale")["pocket_revenue_cents"].sum()
    )


# ---------------------------------------------------------------------------
# Exit gate: reported impact == recomputed baseline-vs-scenario ledger.
# ---------------------------------------------------------------------------


def test_flat_rate_impact_equals_recomputed_ledger(tmp_path: Path) -> None:
    """2% settlement program on net purchases. Baseline 900.00 (settled 18.00);
    candidate 950.00 (settled 19.00). Ledger pocket delta = 49.00 = the
    engine's level-2 figure gap x qty x (1 - d) exactly."""
    base = _world(
        tmp_path / "b",
        unit_price="90.00",
        net="900.00",
        flat_rate="2.0",
        settle=[{"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RPS", "AMT": "18.00"}],
    )
    cand = _world(
        tmp_path / "c",
        unit_price="95.00",
        net="950.00",
        flat_rate="2.0",
        settle=[{"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RPS", "AMT": "19.00"}],
    )
    ledger_delta = _pocket(tmp_path / "c2", cand) - _pocket(tmp_path / "b2", base)
    opp = price_opportunities(_guidance(deduction_bp=200), _panel(), _sensitivity(), Rules())
    row = opp.to_dicts()[0]
    assert row["deduction_treatment"] == "flat_rate_exact"
    assert row["gross_price_opportunity_cents"] == ledger_delta == 4_900


def test_fixed_per_unit_impact_equals_recomputed_scenario_ledger() -> None:
    """A fixed per-unit deduction F cancels out of price gaps and lowers both
    levels. The reported level-3 figure equals a scenario ledger recomputed
    from first principles: pocket GP(P, Q) = (P - F - C) x Q."""
    rules = Rules()
    opp = price_opportunities(
        _guidance(fixed_deduction_cents=100),
        _panel(qty=20.0),
        _sensitivity(status="causal_elasticity_supported"),
        rules,
    )
    row = opp.to_dicts()[0]
    q1 = 20.0 * (9500 / 9000) ** -2.0
    scenario_ledger = (9500 - 100 - 6000) * q1 - (9000 - 100 - 6000) * 20.0
    assert row["expected_pocket_gross_profit_change_cents"] == pytest.approx(scenario_ledger, abs=1)


def _ctx(b0_cents: int) -> TieredDeductionContext:
    return TieredDeductionContext(
        bases=pl.DataFrame(
            {"customer_id": ["C1"], "rebate_program_id": ["TIER01"], "basis_cents": [b0_cents]}
        ),
        tiers=pl.DataFrame(
            {
                "rebate_program_id": ["TIER01", "TIER01"],
                "min_amount_cents": [0, 100_000],
                "rate_bp": [100, 200],
            }
        ),
    )


def test_tiered_crossing_impact_equals_recomputed_ledger(tmp_path: Path) -> None:
    """The candidate revenue crosses the 1,000.00 tier: the 2% rate re-rates
    the WHOLE basis. Baseline 980.00 settled at 1% (9.80); candidate 1,030.00
    settled at 2% (20.60). Ledger pocket delta = 5,000 - 1,080 = 3,920 - and
    the recomputed opportunity figure matches it; the flat rate would have
    claimed 5,000."""
    base = _world(
        tmp_path / "b",
        unit_price="98.00",
        net="980.00",
        settle=[],
        tiered=True,
        tiered_settle=[{"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "9.80"}],
    )
    cand = _world(
        tmp_path / "c",
        unit_price="103.00",
        net="1030.00",
        settle=[],
        tiered=True,
        tiered_settle=[{"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "20.60"}],
    )
    ledger_delta = _pocket(tmp_path / "c2", cand) - _pocket(tmp_path / "b2", base)
    assert ledger_delta == 3_920
    opp = price_opportunities(
        _guidance(current_index=0.98, target_index=1.03),
        _panel(),
        _sensitivity(),
        Rules(),
        tiered_ctx=_ctx(98_000),
        tiered_customers={"C1"},
    )
    row = opp.to_dicts()[0]
    assert row["deduction_treatment"] == "recomputed_tiered"
    assert row["gross_price_opportunity_cents"] == ledger_delta
    # The flat-rate figure would have ignored the re-rating entirely.
    flat = price_opportunities(
        _guidance(current_index=0.98, target_index=1.03), _panel(), _sensitivity(), Rules()
    ).to_dicts()[0]
    assert flat["gross_price_opportunity_cents"] == 5_000


def test_tiered_within_tier_impact_equals_recomputed_ledger(tmp_path: Path) -> None:
    """No crossing: baseline 900.00 and candidate 950.00 both sit in the 1%
    tier. Ledger delta = 5,000 - (950 - 900) = 4,950; recomputed figure
    matches."""
    base = _world(
        tmp_path / "b",
        unit_price="90.00",
        net="900.00",
        settle=[],
        tiered=True,
        tiered_settle=[{"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "9.00"}],
    )
    cand = _world(
        tmp_path / "c",
        unit_price="95.00",
        net="950.00",
        settle=[],
        tiered=True,
        tiered_settle=[{"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "9.50"}],
    )
    ledger_delta = _pocket(tmp_path / "c2", cand) - _pocket(tmp_path / "b2", base)
    assert ledger_delta == 4_950
    opp = price_opportunities(
        _guidance(),
        _panel(),
        _sensitivity(),
        Rules(),
        tiered_ctx=_ctx(90_000),
        tiered_customers={"C1"},
    )
    assert opp.to_dicts()[0]["gross_price_opportunity_cents"] == ledger_delta


def test_mixed_flat_plus_tiered_equals_recomputed_ledger(tmp_path: Path) -> None:
    """Mixed arrangement: a 2% flat settlement program AND a tiered program.
    The flat rate flattens into d (same base, independent); the tiered rebate
    is recomputed. Candidate crosses the tier.
    Ledger delta = 5,000 - 2% x 5,000 - (2% x 103,000 - 1% x 98,000)
                 = 5,000 - 100 - 1,080 = 3,820."""
    base = _world(
        tmp_path / "b",
        unit_price="98.00",
        net="980.00",
        flat_rate="2.0",
        settle=[{"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RPS", "AMT": "19.60"}],
        tiered=True,
        tiered_settle=[{"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "9.80"}],
    )
    cand = _world(
        tmp_path / "c",
        unit_price="103.00",
        net="1030.00",
        flat_rate="2.0",
        settle=[{"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RPS", "AMT": "20.60"}],
        tiered=True,
        tiered_settle=[{"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "20.60"}],
    )
    ledger_delta = _pocket(tmp_path / "c2", cand) - _pocket(tmp_path / "b2", base)
    assert ledger_delta == 3_820
    opp = price_opportunities(
        _guidance(current_index=0.98, target_index=1.03, deduction_bp=200),
        _panel(),
        _sensitivity(),
        Rules(),
        tiered_ctx=_ctx(98_000),
        tiered_customers={"C1"},
    )
    assert opp.to_dicts()[0]["gross_price_opportunity_cents"] == ledger_delta


# ---------------------------------------------------------------------------
# Labels and coverage.
# ---------------------------------------------------------------------------


def test_tiered_without_table_is_labeled_approximation() -> None:
    opp = price_opportunities(
        _guidance(), _panel(), _sensitivity(), Rules(), tiered_ctx=None, tiered_customers={"C1"}
    )
    assert opp.to_dicts()[0]["deduction_treatment"] == "fixed_rate_approximation"


def test_level3_recomputed_for_tiered_customers() -> None:
    """The causal level-3 figures also carry the recomputed tiered rebate: the
    candidate revenue P1 x Q1 shifts the basis, and the rebate delta nets out
    of both bases."""
    ctx = _ctx(90_000)
    opp = price_opportunities(
        _guidance(cts_per_unit_cents=200),
        _panel(qty=20.0),
        _sensitivity(status="causal_elasticity_supported"),
        Rules(),
        tiered_ctx=ctx,
        tiered_customers={"C1"},
    )
    row = opp.to_dicts()[0]
    q1 = 20.0 * (9500 / 9000) ** -2.0
    dr3 = 9500 * q1 - 9000 * 20.0
    adj = ctx.delta_cents("C1", dr3)
    assert adj is not None
    gp_flat = (9500 - 6000) * q1 - (9000 - 6000) * 20.0
    assert row["expected_pocket_gross_profit_change_cents"] == pytest.approx(gp_flat - adj, abs=1)
    contrib_flat = (9500 - 6200) * q1 - (9000 - 6200) * 20.0
    assert row["expected_contribution_change_cents"] == pytest.approx(contrib_flat - adj, abs=1)


def test_negative_basis_recomputes_to_zero_rebate() -> None:
    """A negative baseline basis accrues nothing; the recomputation only books
    rebate on the positive candidate basis (fail closed on the way in)."""
    ctx = _ctx(-50_000)
    assert ctx.rebate_cents("TIER01", -50_000) == 0.0
    delta = ctx.delta_cents("C1", 60_000)
    assert delta == pytest.approx(0.01 * 10_000, abs=1)  # 1% of the positive 100.00


def test_deduction_coverage_reports_sources() -> None:
    g = pl.DataFrame(
        {
            "customer_id": ["C1", "C1", "C2"],
            "item_id": ["I1", "I2", "I1"],
            "rebate_bp": [200, 200, 0],
            "concession_bp": [50, 50, 0],
        }
    )
    cov = deduction_coverage(g, {"C2"})
    c1 = cov.filter(pl.col("customer_id") == "C1").to_dicts()[0]
    assert c1["d_total_bp"] == 250
    assert c1["contract_derived_bp"] == 200
    assert c1["observed_bp"] == 50
    assert c1["observed_share"] == pytest.approx(0.2)
    c2 = cov.filter(pl.col("customer_id") == "C2").to_dicts()[0]
    assert c2["d_total_bp"] == 0
    assert c2["observed_share"] == 0.0  # no invented coverage
    assert c2["tiered_recomputed"] is True


def test_build_tiered_context_from_engine_frames(tmp_path: Path) -> None:
    w = _world(
        tmp_path,
        unit_price="98.00",
        net="980.00",
        settle=[],
        tiered=True,
    )
    res = run_pipeline(w.write(), MAPPING, Rules())
    ctx = build_tiered_context(
        res.reference["rebate_programs"],
        res.reference["rebate_program_tiers"],
        res.tiered_summary,
    )
    assert ctx is not None
    assert ctx.bases.to_dicts()[0]["basis_cents"] == 98_000
    assert ctx.rate_bp("TIER01", 98_000) == 100
    assert ctx.rate_bp("TIER01", 103_000) == 200
