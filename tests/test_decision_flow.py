"""The decision-flow surface: one verdict, and a staged first step.

A pricing manager reading an opportunity row has three questions - what
should I do, how big is it, how sure are we - and used to need two technical
fields (evidence status, volume treatment) to answer the third. The engine
now answers it once, as a verdict, and where the full move to target reaches
beyond the identified price range it also names the move it CAN forecast:
step 1 of the move to target.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import polars as pl

from margin_engine.actions.generate import generate_actions
from margin_engine.config import Rules, load_rules
from margin_engine.opportunity.sizing import (
    VERDICT_FORECAST,
    VERDICT_LOW_EVIDENCE,
    VERDICT_UPSIDE_ONLY,
    price_opportunities,
    verdict_for,
)
from margin_engine.webexport import explain

REPO = Path(__file__).resolve().parents[1]


def _sensitivity(
    *,
    status: str = "causal_elasticity_supported",
    lo: float = -0.10,
    hi: float = 0.10,
    shape: str = "constant_elasticity_supported",
) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "segment": 1,
                "iv": -1.5,
                "status": status,
                "iv_ci_low": -1.7,
                "iv_ci_high": -1.3,
                "support_log_lo": lo,
                "support_log_hi": hi,
                "functional_form_status": shape,
                "competitor_path_resolution": "controlled_by_market_index",
            }
        ]
    )


def _guidance(target_index: float = 0.95) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
                "segment_leaf": 1,
                "guidance_status": "below_reference",
                "target_index": target_index,
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
        ]
    )


def _panel() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
                "segment_leaf": 1,
                "month_idx": 24300,
                "qty": 20.0,
                "price_cf": 9000.0,
                "supplier_id": "S1",
                "item_category": "CATA",
                "contract_id": None,
            }
        ]
    ).with_columns(pl.col("contract_id").cast(pl.String))


def _sized(sens: pl.DataFrame, target_index: float = 0.95) -> dict[str, Any]:
    return price_opportunities(_guidance(target_index), _panel(), sens, Rules()).to_dicts()[0]


# ---------------------------------------------------------------------------
# The staged first step
# ---------------------------------------------------------------------------
# The pair is priced at 0.90 of a $100.00 list and the target is 0.95: a
# +5.4% move. Support that stops short of it forces the step.


def test_a_move_beyond_support_gets_a_first_step_with_its_own_forecast() -> None:
    row = _sized(_sensitivity(lo=-0.03, hi=0.03))
    assert row["volume_treatment"] == "outside_price_support"
    assert row["expected_pocket_gross_profit_change_cents"] is None
    # The step: the largest supported move, strictly between here and target.
    assert row["step_price_cents"] is not None
    assert row["current_invoice_price_cents"] < row["step_price_cents"]
    assert row["step_price_cents"] < row["proposed_invoice_price_cents"]
    assert 0 < row["step_move_pct"] < 0.054
    assert abs(row["step_move_pct"] - 0.0305) < 0.002  # exp(0.03) - 1
    assert row["step_expected_pocket_gross_profit_change_cents"] is not None
    # A step up at beta -1.5 on margin-positive economics earns money.
    assert row["step_expected_pocket_gross_profit_change_cents"] > 0


def test_a_fully_supported_move_has_no_step_to_name() -> None:
    row = _sized(_sensitivity(lo=-0.5, hi=0.5))
    assert row["volume_treatment"] == "expected_change"
    assert row["step_price_cents"] is None


def test_the_step_respects_the_narrowed_band_of_an_untested_shape() -> None:
    """Shape untested narrows what may be forecast to half the range; the
    step must stop at the narrowed edge, not the full one."""
    full = _sized(_sensitivity(lo=-0.06, hi=0.06, shape="functional_form_inconclusive"))
    assert full["volume_treatment"] == "price_response_shape_untested"
    assert full["step_price_cents"] is not None
    # Half of [-0.06, 0.06] around its midpoint reaches +0.03.
    assert abs(full["step_move_pct"] - 0.0305) < 0.002


def test_no_step_without_a_usable_elasticity() -> None:
    row = _sized(_sensitivity(status="iv_estimated_elasticity", lo=-0.03, hi=0.03))
    assert row["step_price_cents"] is None


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def test_the_three_verdicts_cover_the_table() -> None:
    assert _sized(_sensitivity(lo=-0.5, hi=0.5))["verdict"] == VERDICT_FORECAST
    assert _sized(_sensitivity(lo=-0.001, hi=0.001))["verdict"] == VERDICT_UPSIDE_ONLY
    assert (
        _sized(_sensitivity(status="insufficient_data", lo=-0.5, hi=0.5))["verdict"]
        == VERDICT_LOW_EVIDENCE
    )


def test_every_verdict_without_a_forecast_says_why() -> None:
    row = _sized(_sensitivity(lo=-0.001, hi=0.001))
    assert row["verdict_reason"]
    assert "_" not in row["verdict_reason"]


def test_the_scalar_verdict_matches_the_vectorized_one() -> None:
    """`verdict_for` is what action rows use; the columns are what opportunity
    rows use. They must be the same classification."""
    for sens in (
        _sensitivity(lo=-0.5, hi=0.5),
        _sensitivity(lo=-0.001, hi=0.001),
        _sensitivity(status="insufficient_data"),
        _sensitivity(status="iv_estimated_elasticity"),
        _sensitivity(lo=-0.06, hi=0.06, shape="functional_form_inconclusive"),
    ):
        row = _sized(sens)
        scalar = verdict_for(
            evidence_status=row["evidence_status"],
            has_forecast=row["volume_treatment"] == "expected_change",
            cost_unavailable=bool(row["cost_unavailable"]),
        )
        assert scalar == row["verdict"], row["volume_treatment"]


# ---------------------------------------------------------------------------
# The decision sentence and the size line
# ---------------------------------------------------------------------------


def test_rows_lead_with_the_decision_and_state_the_size_honestly() -> None:
    step = _sized(_sensitivity(lo=-0.03, hi=0.03))
    assert "start with +3%" in explain.opportunity_decision(step)
    size = explain.opportunity_size(step)
    assert "no forecast beyond +3%" in size
    assert "if no customers react" in size

    forecast = _sized(_sensitivity(lo=-0.5, hi=0.5))
    assert "to the target" in explain.opportunity_decision(forecast)
    assert "allowing for customers buying less" in explain.opportunity_size(forecast)

    upside = _sized(_sensitivity(status="iv_estimated_elasticity", lo=-0.5, hi=0.5))
    assert "no volume forecast" in explain.opportunity_decision(upside)
    assert "if every customer keeps buying the same" in explain.opportunity_size(upside)


# ---------------------------------------------------------------------------
# The step as the action's proposal
# ---------------------------------------------------------------------------


def _actions_for(sens: pl.DataFrame) -> pl.DataFrame:
    rules = load_rules(REPO / "config" / "rules.yaml")
    guidance = _guidance().with_columns(
        pl.lit(0.6316).alias("floor_index"),
        pl.lit(value=False).alias("rp_review_flag"),
    )
    opportunities = price_opportunities(guidance, _panel(), sens, rules)
    ledger = pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
                "transaction_type": "sale",
                "uom_norm": "EA",
                "invoice_date": dt.date(2026, 10, 1),
            }
        ]
    )
    result = generate_actions(
        ledger=ledger,
        guidance=guidance,
        opportunities=opportunities,
        leakage=pl.DataFrame(),
        service_flags=None,
        sensitivity=sens,
        panel=None,
        rules=rules,
    )
    return result.actions


def test_the_action_proposes_the_step_where_the_full_move_has_no_forecast() -> None:
    actions = _actions_for(_sensitivity(lo=-0.03, hi=0.03)).filter(
        pl.col("action_type") == "price_change"
    )
    assert actions.height == 1
    a = actions.to_dicts()[0]
    assert a["staged_step"] is True
    assert "step 1 of the move to target" in a["title"]
    assert "step 1 of the move to target" in a["rationale"]
    # It proposes the step, carries the unchanged full target, and its impact
    # is a forecast - which is the whole point.
    assert a["proposed_price_cents"] < a["full_target_price_cents"]
    assert a["impact_basis"].startswith("expected")
    assert a["expected_impact_cents"] is not None
    assert a["predicted_volume_ratio"] < 1.0


def test_a_supported_move_still_proposes_the_full_target() -> None:
    actions = _actions_for(_sensitivity(lo=-0.5, hi=0.5)).filter(
        pl.col("action_type") == "price_change"
    )
    a = actions.to_dicts()[0]
    assert a["staged_step"] is False
    assert a["full_target_price_cents"] is None
    assert "step 1" not in a["title"]
    assert a["impact_basis"].startswith("expected")


def test_an_unforecastable_pair_without_a_step_stays_fixed_volume() -> None:
    actions = _actions_for(
        _sensitivity(status="iv_estimated_elasticity", lo=-0.001, hi=0.001)
    ).filter(pl.col("action_type") == "price_change")
    a = actions.to_dicts()[0]
    assert a["staged_step"] is False
    assert a["impact_basis"] == "gross_price_opportunity_fixed_volume"
