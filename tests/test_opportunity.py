"""Phase 7 verification: hand-calculable scenario math, three-level separation,
overlap handling without double counting, rollup reconciliation.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from margin_engine.config import Rules
from margin_engine.opportunity.sizing import optimize_targets, price_opportunities
from margin_engine.scenarios.tariff import (
    CostShock,
    combined_scenario_no_double_count,
    run_cost_scenario,
)


def _panel(rows: list[dict]) -> pl.DataFrame:
    base = {
        "customer_id": "C1",
        "item_id": "I1",
        "segment_leaf": 1,
        "month_idx": 24300,
        "qty": 10.0,
        "price_cf": 9000.0,
        "supplier_id": "S1",
        "item_category": "CATA",
        "contract_id": None,
    }
    return pl.DataFrame([{**base, **r} for r in rows]).with_columns(
        pl.col("contract_id").cast(pl.String)
    )


def _guidance(rows: list[dict]) -> pl.DataFrame:
    base = {
        "customer_id": "C1",
        "item_id": "I1",
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
        # Pocket parameters (24.1): percentage deductions d (basis points) and
        # fixed per-unit deductions F (cents). Zero keeps invoice == pocket.
        "deduction_bp": 0,
        "fixed_deduction_cents": 0,
    }
    return pl.DataFrame([{**base, **r} for r in rows])


def _sensitivity(
    status: str = "causal_elasticity_supported",
    beta: float = -2.0,
    support: tuple[float, float] = (-0.5, 0.5),
) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "segment": 1,
                "iv": beta,
                "iv_ci_low": beta - 0.2,
                "iv_ci_high": beta + 0.2,
                "status": status,
                "support_log_lo": support[0],
                "support_log_hi": support[1],
            }
        ]
    )


def test_gross_price_opportunity_hand_calc() -> None:
    # qty 20 over window; gap = (0.95 - 0.90) x $100.00 = $5.00/unit -> $100.00.
    panel = _panel([{"qty": 12.0, "month_idx": 24300}, {"qty": 8.0, "month_idx": 24301}])
    opp = price_opportunities(_guidance([{}]), panel, _sensitivity(), Rules())
    row = opp.to_dicts()[0]
    assert row["baseline_qty"] == 20.0
    assert row["gap_invoice_cents"] == pytest.approx(500.0)
    assert row["gross_price_opportunity_cents"] == 10_000


def test_expected_contribution_change_causal_hand_calc() -> None:
    # P0=9000c (0.90 x 10000), P1=9500c, C=6000c, beta=-2, Q0=20.
    # Q1 = 20 x (9500/9000)^-2 = 20 x 0.897507 = 17.95014
    # delta = (9500-6000) x 17.95014 - (9000-6000) x 20 = 62825.5 - 60000 = 2826
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(_guidance([{}]), panel, _sensitivity(), Rules())
    row = opp.to_dicts()[0]
    assert row["volume_treatment"] == "expected_change"
    assert row["expected_contribution_change_cents"] == pytest.approx(2826, abs=2)


def test_iv_status_gives_range_only() -> None:
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(
        _guidance([{}]), panel, _sensitivity(status="iv_estimated_elasticity"), Rules()
    )
    row = opp.to_dicts()[0]
    assert row["volume_treatment"] == "sensitivity_range_only"
    assert row["expected_contribution_change_cents"] is None
    assert row["contribution_change_low_cents"] is not None
    assert row["contribution_change_low_cents"] <= row["contribution_change_high_cents"]


def test_descriptive_status_fixed_volume_only() -> None:
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(
        _guidance([{}]), panel, _sensitivity(status="descriptive_elasticity"), Rules()
    )
    row = opp.to_dicts()[0]
    assert row["volume_treatment"] == "fixed_volume_only"
    assert row["expected_contribution_change_cents"] is None
    assert row["contribution_change_low_cents"] is None
    assert row["gross_price_opportunity_cents"] == 10_000  # level 2 still present


def test_contract_protected_excluded() -> None:
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(
        _guidance([{"contract_protected": True}]), panel, _sensitivity(), Rules()
    )
    assert opp.height == 0


def test_negative_gap_not_an_opportunity() -> None:
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(_guidance([{"current_index": 0.99}]), panel, _sensitivity(), Rules())
    assert opp.height == 0


def test_every_record_carries_trace_fields() -> None:
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(_guidance([{}]), panel, _sensitivity(), Rules())
    row = opp.to_dicts()[0]
    for fld in (
        "baseline_window",
        "action",
        "constraints",
        "quantity_basis",
        "calc_trace",
        "evidence_status",
    ):
        assert row[fld]


def test_optimize_monopoly_markup_clipped() -> None:
    # beta=-2, C=6000 -> P* = 6000 x 2 = 12000; corridor top = 0.97x10000=9700;
    # clipped to 9700 (constraints bind).
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(_guidance([{}]), panel, _sensitivity(beta=-2.0), Rules())
    opp = opp.with_columns(pl.lit(-2.0).alias("elasticity"))
    out = optimize_targets(opp, _guidance([{}]), Rules())
    row = out.to_dicts()[0]
    assert row["target_basis"] == "optimized_monopoly_markup_clipped"
    assert row["optimal_price_cents"] == 9_700


def test_optimize_only_when_causal() -> None:
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(
        _guidance([{}]), panel, _sensitivity(status="iv_estimated_elasticity"), Rules()
    )
    opp = opp.with_columns(pl.lit(-2.0).alias("elasticity"))
    out = optimize_targets(opp, _guidance([{}]), Rules())
    row = out.to_dicts()[0]
    assert row["target_basis"] == "provisional_historical"
    assert row["optimal_price_cents"] is None


# ---------------------------------------------------------------------------
# Cost / tariff scenarios
# ---------------------------------------------------------------------------


def test_pass_through_strategies_reconcile() -> None:
    # Two pairs, one protected. +10% on cost 6000 -> 600c/unit.
    panel = _panel(
        [
            {"qty": 10.0},
            {"customer_id": "C2", "qty": 5.0, "contract_id": "K1"},
        ]
    )
    guidance = _guidance([{}, {"customer_id": "C2"}])
    shock = CostShock("tariff_A", "category", "CATA", 0.10, date(2025, 1, 1))
    out = run_cost_scenario(panel, guidance, _sensitivity(), shock, Rules())
    d = {r["strategy"]: r for r in out.to_dicts()}
    # Incremental cost: (10 + 5) x 600 = 9000c in every strategy.
    for s in ("none", "partial_50", "full"):
        assert d[s]["incremental_cost_cents"] == 9_000
        # recovered + residual == incremental, always.
        assert d[s]["recovered_cents"] + d[s]["residual_exposure_cents"] == 9_000
    assert d["none"]["recovered_cents"] == 0
    # Full pass-through recovers only the unprotected pair: 10 x 600 = 6000.
    assert d["full"]["recovered_cents"] == 6_000
    assert d["partial_50"]["recovered_cents"] == 3_000
    # Protected exposure visible.
    assert d["full"]["protected_exposure_cents"] == 3_000
    # Contribution effect present under causal status for full pass-through.
    assert d["full"]["contribution_effect_cents"] is not None


def test_overlapping_scenarios_not_double_counted() -> None:
    # Supplier shock and category shock both hit I1 (S1, CATA). Joint model
    # compounds: (1.1 x 1.05 - 1) x 6000 x 10 = 9300c — NOT 600x10 + 300x10 = 9000
    # summed separately per scenario on the same pair, and the pair appears once.
    panel = _panel([{"qty": 10.0}])
    guidance = _guidance([{}])
    s1 = CostShock("sup", "supplier", "S1", 0.10, date(2025, 1, 1))
    s2 = CostShock("cat", "category", "CATA", 0.05, date(2025, 1, 1))
    joint = combined_scenario_no_double_count(panel, guidance, _sensitivity(), [s1, s2], Rules())
    row = joint.to_dicts()[0]
    assert row["n_pairs"] == 1
    assert row["incremental_cost_cents"] == pytest.approx((1.1 * 1.05 - 1) * 6000 * 10, abs=1)


def test_three_levels_never_summed() -> None:
    """Structural check: the output schema keeps the three levels as separate
    columns with no combined total column."""
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(_guidance([{}]), panel, _sensitivity(), Rules())
    assert "gross_price_opportunity_cents" in opp.columns
    assert "expected_contribution_change_cents" in opp.columns
    assert not any("total_opportunity" in c for c in opp.columns)


def test_pocket_aware_gross_and_contribution_hand_calc() -> None:
    """24.1: d=10%, F=0. Invoice gap 500c retains 450c pocket per unit.
    Gross = 20 x 500 x 0.9 = 9000c.
    Unit contribution c(P) = 0.9P - 6000: c0 = 2100, c1 = 2550.
    Q1 = 20 x (9500/9000)^-2 = 17.950138...
    delta = 2550 x 17.950138 - 2100 x 20 = 45772.85 - 42000 = 3773c."""
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(_guidance([{"deduction_bp": 1_000}]), panel, _sensitivity(), Rules())
    row = opp.to_dicts()[0]
    assert row["gross_price_opportunity_cents"] == 9_000
    assert row["expected_contribution_change_cents"] == pytest.approx(3_773, abs=2)
    assert row["pocket_retention"] == pytest.approx(0.90)


def test_fixed_deduction_cancels_in_gap_not_in_level() -> None:
    """F cancels out of price GAPS (same F at P0 and P1) but lowers both
    contribution LEVELS: with F=100c, d=0, delta changes only through Q1.
    c0 = 9000-100-6000 = 2900; c1 = 9500-100-6000 = 3400.
    delta = 3400 x 17.950138 - 2900 x 20 = 61030.5 - 58000 = 3030c."""
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(
        _guidance([{"fixed_deduction_cents": 100}]), panel, _sensitivity(), Rules()
    )
    row = opp.to_dicts()[0]
    assert row["gross_price_opportunity_cents"] == 10_000  # F cancels in the gap
    assert row["expected_contribution_change_cents"] == pytest.approx(3_031, abs=2)


def test_missing_cost_never_zero() -> None:
    """24.2: a pair without unit cost keeps its price-based level 2 but is
    excluded from cost-dependent levels and labeled cost_unavailable."""
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(
        _guidance([{"unit_cost_cents": None}]), panel, _sensitivity(), Rules()
    )
    row = opp.to_dicts()[0]
    assert row["volume_treatment"] == "cost_unavailable"
    assert row["cost_unavailable"] is True
    assert row["gross_price_opportunity_cents"] == 10_000
    assert row["expected_contribution_change_cents"] is None
    assert row["contribution_change_low_cents"] is None


def test_outside_price_support_no_volume_response() -> None:
    """24.6: the proposed move log(9500/9000)=0.054 lies outside a tight
    identifying range, so even a causal segment gets fixed-volume only."""
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(_guidance([{}]), panel, _sensitivity(support=(-0.01, 0.01)), Rules())
    row = opp.to_dicts()[0]
    assert row["volume_treatment"] == "outside_price_support"
    assert row["expected_contribution_change_cents"] is None
    assert row["contribution_change_low_cents"] is None
    assert row["gross_price_opportunity_cents"] == 10_000


def test_conservative_level2_below_p25_to_p25() -> None:
    """24.13: the conservative variant raises below-P25 pairs to P25 only.
    current 0.90 < P25 0.92: gap = 0.02 x 10000 = 200c x 20 = 4000c."""
    panel = _panel([{"qty": 20.0}])
    opp = price_opportunities(_guidance([{}]), panel, _sensitivity(), Rules())
    row = opp.to_dicts()[0]
    assert row["gross_price_opportunity_conservative_cents"] == 4_000
    # A pair already at or above P25 contributes zero to the conservative level.
    opp2 = price_opportunities(
        _guidance([{"current_index": 0.93, "target_index": 0.96}]),
        panel,
        _sensitivity(),
        Rules(),
    )
    assert opp2.to_dicts()[0]["gross_price_opportunity_conservative_cents"] == 0


def test_optimal_price_pocket_aware() -> None:
    """24.1: P* = (C + F)/(1 - d) x beta/(1+beta). With d=10%, F=0, C=6000,
    beta=-2: P* = 6666.67 x 2 = 13333 -> clipped to corridor top 9700."""
    panel = _panel([{"qty": 20.0}])
    g = _guidance([{"deduction_bp": 1_000}])
    opp = price_opportunities(g, panel, _sensitivity(beta=-2.0), Rules())
    opp = opp.with_columns(pl.lit(-2.0).alias("elasticity"))
    out = optimize_targets(opp, g, Rules())
    row = out.to_dicts()[0]
    assert row["target_basis"] == "optimized_monopoly_markup_clipped"
    assert row["optimal_price_cents"] == 9_700


def test_optimal_price_interior_pocket_aware() -> None:
    """Interior optimum: beta=-3 -> P* = (C+F)/(1-d) x 1.5. With C=6000,
    F=120, d=10%: c_eff = 6800, P* = 10200 -> above corridor top 11640? No:
    corridor top = 0.97 x 12000 = 11640, floor 6316 -> P* = 10200 interior."""
    panel = _panel([{"qty": 20.0}])
    g = _guidance([{"deduction_bp": 1_000, "fixed_deduction_cents": 120, "eff_list_cents": 12_000}])
    opp = price_opportunities(g, panel, _sensitivity(beta=-3.0), Rules())
    opp = opp.with_columns(pl.lit(-3.0).alias("elasticity"))
    out = optimize_targets(opp, g, Rules())
    row = out.to_dicts()[0]
    assert row["target_basis"] == "optimized_monopoly_markup_clipped"
    assert row["optimal_price_cents"] == 10_200


def test_optimize_skips_missing_cost() -> None:
    """24.2: no cost, no optimization - provisional target kept."""
    panel = _panel([{"qty": 20.0}])
    g = _guidance([{"unit_cost_cents": None}])
    opp = price_opportunities(g, panel, _sensitivity(), Rules())
    opp = opp.with_columns(pl.lit(-2.0).alias("elasticity"))
    out = optimize_targets(opp, g, Rules())
    row = out.to_dicts()[0]
    assert row["target_basis"] == "provisional_historical"
    assert row["optimal_price_cents"] is None


def test_observed_leakage_reconciles_to_ledger() -> None:
    from datetime import date as _date

    from margin_engine.opportunity.sizing import observed_leakage

    ledger = pl.DataFrame(
        {
            "customer_id": ["C1", "C1", "C2"],
            "transaction_type": ["sale", "sale", "sale"],
            "invoice_date": pl.Series([_date(2025, 10, 1)] * 3, dtype=pl.Date),
            "net_amount_cents": [100_000, 50_000, 80_000],
            "discount_cents": [10_000, -500, 4_000],  # negative = surcharge, not leakage
            "rebate_actual_cents": [3_000, 1_500, 0],
            "rebate_settlement_alloc_cents": [1_000, 500, 800],
            "freight_concession_cents": [500, 0, 250],
            "price_credit_alloc_cents": [-750, 0, 0],
        }
    )
    out = observed_leakage(ledger, Rules())
    c1 = out.filter(pl.col("customer_id") == "C1").to_dicts()[0]
    assert c1["on_invoice_discount_cents"] == 10_000  # surcharge excluded
    assert c1["total_leakage_cents"] == 10_000 + 4_500 + 1_500 + 500 + 750
