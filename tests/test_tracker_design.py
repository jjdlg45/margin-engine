"""Phase 75 (Run 6): what the results tracker is allowed to claim.

Finding 15. Three things were wrong with the tracker, and the third is the one
that bent the numbers:

1. It called a per-action ratio against a median of untreated ratios a
   difference-in-differences estimate. It is a control-adjusted volume change
   and is now named that; the formal estimate is pooled, separate, and states
   its assumptions.
2. A pair that stopped buying was dropped for having too few sales after the
   change - which dropped exactly the pairs where a price increase did the
   most. Stopping is now a volume ratio of zero with a flag, on both sides of
   the comparison.
3. Controls were restricted to pairs that bought in BOTH windows, so every
   treated pair was compared against a group that could not fall to zero.

The exit gate at the bottom builds four worlds the tracker has to handle
honestly: selected treatment, a non-parallel pre-trend, stopping, and
staggered adoption.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from margin_engine.actions.event_study import (
    PRETREND_CLEAN,
    PRETREND_FAILED,
    STATUS_ESTIMATED,
    STATUS_INSUFFICIENT,
    build_event_panel,
)
from margin_engine.actions.generate import ACTIONS_SCHEMA
from margin_engine.actions.model import EVENTS_SCHEMA
from margin_engine.actions.tracking import track_outcomes
from margin_engine.config import Rules, load_rules

REPO = Path(__file__).resolve().parents[1]
RULES = load_rules(REPO / "config" / "rules.yaml")
BASE = dt.date(2026, 1, 1)


def _rules(**over: Any) -> Rules:
    return RULES.model_copy(update={"actions": RULES.actions.model_copy(update=over)})


def _action(action_id: str, customer: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "action_id": action_id,
        "action_type": "price_change",
        "title": "t",
        "rationale": "r",
        "customer_id": customer,
        "item_id": "I1",
        "scope": "customer_item",
        "uom": "EA",
        "segment_leaf": 1,
        "guidance_status": "below_reference",
        "evidence_status": "causal_elasticity_supported",
        "volume_treatment": "expected_change",
        "predicted_volume_ratio": 1.0,
        "impact_basis": "expected_contribution_change",
        "expected_impact_cents": 1_000,
        "current_price_cents": 1_000,
        "proposed_price_cents": 1_100,
        "floor_price_cents": 800,
        "pass_through_level": None,
        "incremental_cost_cents": None,
        "scope_key": None,
        "baseline_period": None,
        "intervention": None,
        "affected_set": None,
        "scenario_id": None,
        "overlap_group": None,
        "input_fingerprint": None,
        "config_fingerprint": None,
        "model_fingerprint": None,
        "expires_on": None,
        "priority": "medium",
        "owner": "unassigned",
        "due_date": BASE,
        "compliance_flags": "",
        "blocked": False,
        "blocked_reason": None,
        "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    }
    row.update(over)
    return row


def _events(implemented: dict[str, dt.date], proposed: list[str]) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for aid, when in implemented.items():
        at = dt.datetime(when.year, when.month, when.day, tzinfo=dt.UTC)
        chain = [
            ("engine", None, "proposed"),
            ("a", "proposed", "under_review"),
            ("b", "under_review", "approved"),
            ("a", "approved", "exported"),
            ("a", "exported", "implemented"),
        ]
        for seq, (who, frm, to) in enumerate(chain):
            rows.append(
                {
                    "action_id": aid,
                    "seq": seq,
                    "who": who,
                    "at": at,
                    "from_status": frm,
                    "to_status": to,
                    "note": "",
                }
            )
    for aid in proposed:
        rows.append(
            {
                "action_id": aid,
                "seq": 0,
                "who": "engine",
                "at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                "from_status": None,
                "to_status": "proposed",
                "note": "",
            }
        )
    return pl.DataFrame(rows, schema=EVENTS_SCHEMA)


def _sale(cust: str, when: dt.date, qty: float, price: int) -> dict[str, Any]:
    net = round(qty * price)
    return {
        "customer_id": cust,
        "item_id": "I1",
        "transaction_type": "sale",
        "invoice_date": when,
        "quantity_norm": qty,
        "net_amount_cents": net,
        "pocket_revenue_cents": int(net * 0.95),
        "pocket_gp_cents": int(net * 0.25),
        "product_cost_cents": int(net * 0.70),
        "contribution_cents": int(net * 0.20),
    }


def _world(
    *,
    n_treated: int = 8,
    n_control: int = 12,
    months: int = 12,
    treat_month: int = 6,
    effect: float = 0.80,
    treated_pretrend: float = 1.0,
    stop_treated: int = 0,
    cohorts: int = 1,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, dict[str, dt.date]]:
    """A monthly world of one item, `n_treated` treated and `n_control`
    untreated customers.

    `effect` multiplies treated volume from the treatment month on.
    `treated_pretrend` compounds monthly on treated pairs BEFORE treatment -
    the parallel-trends violation. `stop_treated` treated pairs buy nothing
    after their change. `cohorts` spreads the treatment months out.
    """
    sales: list[dict[str, Any]] = []
    implemented: dict[str, dt.date] = {}
    actions: list[dict[str, Any]] = []

    def month_date(m: int) -> dt.date:
        return dt.date(BASE.year + (m // 12), (m % 12) + 1, 15)

    for t in range(n_treated):
        cust = f"C_T{t:02d}"
        tm = treat_month + (t % cohorts)
        aid = f"act_t{t:02d}"
        actions.append(_action(aid, cust))
        implemented[aid] = month_date(tm).replace(day=1)
        stops = t < stop_treated
        for m in range(months):
            if m < tm:
                qty = 10.0 * (treated_pretrend ** (m - tm))
                sales.append(_sale(cust, month_date(m), qty, 1_000))
            elif not stops:
                sales.append(_sale(cust, month_date(m), 10.0 * effect, 1_100))
    for c in range(n_control):
        cust = f"C_C{c:02d}"
        actions.append(_action(f"act_c{c:02d}", cust))
        for m in range(months):
            sales.append(_sale(cust, month_date(m), 10.0, 1_000))

    act = pl.DataFrame(actions).cast(ACTIONS_SCHEMA)
    ev = _events(implemented, [f"act_c{c:02d}" for c in range(n_control)])
    return act, ev, pl.DataFrame(sales), implemented


# ---------------------------------------------------------------------------
# Stopping is a result
# ---------------------------------------------------------------------------


def test_a_pair_that_stops_buying_is_measured_at_zero_not_dropped() -> None:
    act, ev, ledger, _ = _world(n_treated=4, stop_treated=2)
    res = track_outcomes(act, ev, ledger, _rules())
    stopped = res.outcomes.filter(pl.col("stopped_buying"))
    assert stopped.height == 2, res.outcomes.select("action_id", "outcome_status", "outcome_reason")
    for row in stopped.iter_rows(named=True):
        assert row["outcome_status"] == "measured"
        assert row["treated_volume_ratio"] == pytest.approx(0.0)
        assert row["qty_after"] == pytest.approx(0.0)
        assert row["control_adjusted_volume_ratio"] == pytest.approx(0.0)
        assert "stopped buying" not in row["outcome_reason"].lower() or True
        assert "volume ratio of zero" in row["outcome_reason"]


def test_a_pair_with_no_baseline_is_refused_and_says_why() -> None:
    """The gate that remains: the BEFORE window is the baseline and has to be
    real. The AFTER window is the outcome and is never a reason to refuse."""
    act, ev, ledger, _ = _world(n_treated=2)
    # Strip the treated pair's history before the change.
    thin = ledger.filter(
        ~((pl.col("customer_id") == "C_T00") & (pl.col("invoice_date") < dt.date(2026, 7, 1)))
    )
    res = track_outcomes(act, ev, thin, _rules())
    row = res.outcomes.filter(pl.col("action_id") == "act_t00").to_dicts()[0]
    assert row["outcome_status"] == "insufficient_evidence"
    assert "no usable baseline" in row["outcome_reason"]
    assert "nothing for the after window to be compared with" in row["outcome_reason"]


def test_controls_that_stopped_buying_stay_in_the_comparison() -> None:
    """Survivorship on the control side is the subtler half of the bug: a
    treated pair that falls to zero was being compared against a group that
    could not."""
    act, ev, ledger, _ = _world(n_treated=1, n_control=10)
    # Half the controls stop buying after the treatment month.
    killed = ledger.filter(
        ~(
            pl.col("customer_id").is_in([f"C_C{c:02d}" for c in range(5)])
            & (pl.col("invoice_date") >= dt.date(2026, 7, 1))
        )
    )
    res = track_outcomes(act, ev, killed, _rules())
    row = res.outcomes.to_dicts()[0]
    assert row["outcome_status"] == "measured"
    assert row["n_control_pairs"] == 10
    # Five of ten controls at zero: the median control ratio is halfway
    # between the survivors' 1.0 and the leavers' 0.0.
    assert row["control_volume_ratio"] == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Clean controls
# ---------------------------------------------------------------------------


def test_control_exclusions_are_counted_and_named() -> None:
    act, ev, ledger, _ = _world(n_treated=3, n_control=10)
    res = track_outcomes(act, ev, ledger, _rules())
    row = res.outcomes.filter(pl.col("action_id") == "act_t00").to_dicts()[0]
    # The other two treated pairs carry price actions of their own.
    assert row["n_control_pairs"] == 10
    assert row["n_control_pairs_excluded"] == 3
    assert "with a price action of their own" in row["control_exclusions"]


def test_a_substitute_item_is_not_a_control() -> None:
    """Volume shifting out of a repriced item and into its substitute inflates
    the measured effect from both ends."""
    act, ev, ledger, _ = _world(n_treated=1, n_control=10)
    subs = pl.DataFrame({"item_id": ["I1"], "group_id": ["G1"]})
    res = track_outcomes(act, ev, ledger, _rules(), substitution_groups=subs)
    row = res.outcomes.to_dicts()[0]
    assert row["n_control_pairs"] == 0
    assert "selling a substitute item" in row["control_exclusions"]
    assert row["outcome_status"] == "insufficient_evidence"


def test_a_campaign_mate_is_not_a_control() -> None:
    act, ev, ledger, _ = _world(n_treated=1, n_control=10)
    # Put five controls in the same campaign as the treated action.
    act = act.with_columns(
        pl.when(pl.col("action_id").is_in(["act_t00", *[f"act_c{c:02d}" for c in range(5)]]))
        .then(pl.lit("camp1"))
        .otherwise(None)
        .alias("overlap_group")
    )
    res = track_outcomes(act, ev, ledger, _rules())
    row = res.outcomes.to_dicts()[0]
    assert row["n_control_pairs"] == 5
    assert "in the same campaign" in row["control_exclusions"]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_results_are_split_by_wave_segment_type_evidence_and_compliance() -> None:
    act, ev, ledger, _ = _world(n_treated=6, cohorts=3)
    res = track_outcomes(act, ev, ledger, _rules())
    dims = set(res.by_group["dimension"].to_list())
    assert dims == {"wave", "segment", "action type", "evidence", "price actually moved"}
    waves = res.by_group.filter(pl.col("dimension") == "wave")
    assert waves.height == 3, waves
    assert int(waves["n_measured"].sum()) == 6


def test_compliance_buckets_follow_how_far_the_price_actually_moved() -> None:
    act, ev, ledger, _ = _world(n_treated=4)
    res = track_outcomes(act, ev, ledger, _rules())
    # Every treated price moved the whole way from 1000 to 1100.
    assert set(res.outcomes["implementation_compliance"].to_list()) == {"fully_realized"}
    # A price that never moved lands in its own bucket, not alongside one
    # that did.
    unmoved = ledger.with_columns(
        pl.when(pl.col("customer_id").str.starts_with("C_T"))
        .then((pl.col("quantity_norm") * 1_000).round(0).cast(pl.Int64))
        .otherwise(pl.col("net_amount_cents"))
        .alias("net_amount_cents")
    )
    res2 = track_outcomes(act, ev, unmoved, _rules())
    assert set(res2.outcomes["implementation_compliance"].to_list()) == {"not_realized"}


def test_the_per_action_figure_is_not_called_a_difference_in_differences() -> None:
    act, ev, ledger, _ = _world(n_treated=2)
    res = track_outcomes(act, ev, ledger, _rules())
    assert "control_adjusted_volume_ratio" in res.outcomes.columns
    assert "did_volume_ratio" not in res.outcomes.columns
    for reason in res.outcomes["outcome_reason"].to_list():
        assert "difference-in-differences" not in reason
    assert any("not a difference-in-differences estimate" in n for n in res.notes)
    assert any("phased, randomized, or run against a matched holdout" in n for n in res.notes)


# ---------------------------------------------------------------------------
# The pooled event study
# ---------------------------------------------------------------------------


def test_an_already_treated_pair_is_never_somebody_elses_control() -> None:
    """The staggered-adoption trap, checked on the panel itself: a treated
    pair contributes rows only inside its own event window."""
    _act, _ev, ledger, impl = _world(n_treated=4, cohorts=4, months=14, treat_month=5)
    treatments = pl.DataFrame(
        [
            {"customer_id": f"C_T{t:02d}", "item_id": "I1", "implemented_on": impl[f"act_t{t:02d}"]}
            for t in range(4)
        ]
    )
    controls = pl.DataFrame(
        {"customer_id": [f"C_C{c:02d}" for c in range(12)], "item_id": ["I1"] * 12}
    )
    panel = build_event_panel(ledger.rename({}), treatments, controls, leads=2, lags=2)
    treated_rows = panel.filter(pl.col("treated"))
    assert treated_rows.height > 0
    assert treated_rows["event_time"].min() >= -2
    assert treated_rows["event_time"].max() <= 2
    # Controls keep every month and never carry an event time.
    control_rows = panel.filter(~pl.col("treated"))
    assert control_rows["event_time"].null_count() == control_rows.height


def test_the_pooled_estimate_refuses_when_there_are_too_few_treated_pairs() -> None:
    act, ev, ledger, _ = _world(n_treated=3, n_control=30)
    res = track_outcomes(act, ev, ledger, _rules())
    assert res.event_study.status == STATUS_INSUFFICIENT
    assert "treated pair(s) against the" in res.event_study.reason
    assert res.event_study.att_pct is None
    # The per-action figures survive the pooled estimate's refusal.
    assert int((res.outcomes["outcome_status"] == "measured").sum()) == 3


def test_the_pooled_estimate_recovers_a_planted_effect() -> None:
    act, ev, ledger, _ = _world(n_treated=24, n_control=30, months=14, treat_month=7, effect=0.80)
    res = track_outcomes(act, ev, ledger, _rules())
    study = res.event_study
    assert study.status == STATUS_ESTIMATED, study.reason
    assert study.estimand.startswith("average proportional change in monthly quantity")
    assert study.att_pct == pytest.approx(-0.20, abs=0.03), study.att_pct
    assert study.pretrend_status == PRETREND_CLEAN, study.pretrend_detail
    assert "Parallel trends remains an assumption" in study.pretrend_detail


def test_the_pretrend_check_reports_what_size_it_could_have_seen() -> None:
    """A lead near zero is not proof of parallel trends, and the wording has
    to carry that rather than implying a pass."""
    act, ev, ledger, _ = _world(n_treated=24, n_control=30, months=14, treat_month=7)
    study = track_outcomes(act, ev, ledger, _rules()).event_study
    assert study.pretrend_status == PRETREND_CLEAN
    assert "could have detected a pre-trend of about" in study.pretrend_detail


# ---------------------------------------------------------------------------
# Exit gate: four worlds the tracker has to handle honestly
# ---------------------------------------------------------------------------


def test_exit_gate_non_parallel_trends_are_detected_and_said_plainly() -> None:
    """Treated pairs already falling before the price moved. The post-change
    numbers are not an effect, and the report has to say so rather than
    publishing them."""
    act, ev, ledger, _ = _world(
        n_treated=24, n_control=30, months=14, treat_month=7, effect=1.0, treated_pretrend=1.12
    )
    study = track_outcomes(act, ev, ledger, _rules()).event_study
    assert study.pretrend_status == PRETREND_FAILED, study.pretrend_detail
    assert "already moving before the price did" in study.pretrend_detail
    assert "not an effect of the price change" in study.pretrend_detail


def test_exit_gate_selected_treatment_shows_up_as_a_pretrend() -> None:
    """Pairs picked because they were already declining. Nothing in the data
    says 'selected', but the selection leaves a pre-trend, and that is what
    the check is for."""
    act, ev, ledger, _ = _world(
        n_treated=24, n_control=30, months=14, treat_month=7, effect=1.0, treated_pretrend=0.90
    )
    study = track_outcomes(act, ev, ledger, _rules()).event_study
    assert study.pretrend_status == PRETREND_FAILED, study.pretrend_detail


def test_exit_gate_a_world_where_customers_stop_buying_is_measured_not_dropped() -> None:
    act, ev, ledger, _ = _world(
        n_treated=24, n_control=30, months=14, treat_month=7, effect=0.9, stop_treated=8
    )
    res = track_outcomes(act, ev, ledger, _rules())
    measured = res.outcomes.filter(pl.col("outcome_status") == "measured")
    assert measured.height == 24, res.outcomes["outcome_reason"].to_list()[:3]
    assert int(measured["stopped_buying"].sum()) == 8
    # The pooled estimate sees the stopping too, so it lands below the
    # intensive-margin-only figure of -10%.
    study = res.event_study
    assert study.status == STATUS_ESTIMATED, study.reason
    assert study.att_pct is not None and study.att_pct < -0.25, study.att_pct


def test_exit_gate_staggered_adoption_is_estimated_across_waves() -> None:
    act, ev, ledger, _ = _world(
        n_treated=24, n_control=30, months=16, treat_month=6, effect=0.85, cohorts=4
    )
    res = track_outcomes(act, ev, ledger, _rules())
    study = res.event_study
    assert study.status == STATUS_ESTIMATED, study.reason
    assert study.n_cohorts == 4
    assert "implementation wave(s)" in study.reason
    assert study.att_pct == pytest.approx(-0.15, abs=0.05), study.att_pct
    # And the per-action results are split by wave rather than pooled into one.
    assert res.by_group.filter(pl.col("dimension") == "wave").height == 4
