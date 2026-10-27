"""Phase 41: results tracking.

Realization math fixtures; the comparison-group (difference-in-differences)
method on planted data; minimum-count gating of the calibration table;
fail-closed gates with reasons; tracker transitions idempotent; the generator's
planted interventions exist and move prices as declared.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from margin_engine.actions.generate import ACTIONS_SCHEMA
from margin_engine.actions.model import EVENTS_SCHEMA, replay_status
from margin_engine.actions.store import ActionStore
from margin_engine.actions.tracking import measure_and_record, track_outcomes
from margin_engine.config import load_rules

REPO = Path(__file__).resolve().parents[1]
RULES = load_rules(REPO / "config" / "rules.yaml")

IMPL = dt.date(2026, 6, 1)
AT = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _action_frame(**over: Any) -> pl.DataFrame:
    row: dict[str, Any] = {
        "action_id": "act_t1",
        "action_type": "price_change",
        "title": "t",
        "rationale": "r",
        "customer_id": "C_T",
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
        "proposed_price_cents": 1_200,
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
        "due_date": IMPL,
        "compliance_flags": "",
        "blocked": False,
        "blocked_reason": None,
        "created_at": AT,
    }
    row.update(over)
    return pl.DataFrame([row]).cast(ACTIONS_SCHEMA)


def _events_for(action_ids: list[str], *, implemented: bool = True) -> pl.DataFrame:
    rows = []
    for aid in action_ids:
        chain = [
            ("engine", None, "proposed"),
            ("demo-analyst", "proposed", "under_review"),
            ("demo-approver", "under_review", "approved"),
            ("demo-analyst", "approved", "exported"),
        ]
        if implemented:
            chain.append(("demo-analyst", "exported", "implemented"))
        for seq, (who, frm, to) in enumerate(chain):
            rows.append(
                {
                    "action_id": aid,
                    "seq": seq,
                    "who": who,
                    "at": AT,
                    "from_status": frm,
                    "to_status": to,
                    "note": "",
                }
            )
    return pl.DataFrame(rows, schema=EVENTS_SCHEMA)


def _ledger(
    *,
    treated_after_ratio: float,
    control_trend: float,
    treated_price_after: int,
    n_controls: int = 6,
    months_of_data: int = 4,
) -> pl.DataFrame:
    """Sales for one treated pair and n controls in segment 1. Before windows
    carry qty 10/month at price 1000; after windows scale qty by the given
    ratios; the treated price moves to treated_price_after."""
    rows = []

    def add(cust: str, when: dt.date, qty: float, price: int) -> None:
        net = round(qty * price)
        rows.append(
            {
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
        )

    for m in range(1, months_of_data):  # before: months -3..-1
        when = IMPL - dt.timedelta(days=30 * m)
        add("C_T", when, 10.0, 1_000)
        for c in range(n_controls):
            add(f"C_CTL{c}", when, 10.0, 1_000)
    for m in range(1, months_of_data):  # after: months +1..+3
        when = IMPL + dt.timedelta(days=30 * m)
        add("C_T", when, 10.0 * control_trend * treated_after_ratio, treated_price_after)
        for c in range(n_controls):
            add(f"C_CTL{c}", when, 10.0 * control_trend, 1_000)
    return pl.DataFrame(rows)


def _control_actions() -> pl.DataFrame:
    """Give every control pair a segment via a proposed (untreated) action."""
    frames = [_action_frame(action_id=f"act_ctl{c}", customer_id=f"C_CTL{c}") for c in range(8)]
    return pl.concat(frames, how="vertical")


def test_realization_and_did_fixture() -> None:
    """Hand numbers: before price 1000, proposed 1200, after 1100 ->
    realization 0.5. Controls x1.3 trend, treated x1.3x0.8 -> DiD 0.8 while
    the raw treated ratio is 1.04 - the trend never masquerades as effect."""
    actions = pl.concat([_action_frame(), _control_actions()], how="vertical")
    events = _events_for(["act_t1"])
    ledger = _ledger(treated_after_ratio=0.8, control_trend=1.3, treated_price_after=1_100)
    res = track_outcomes(actions, events, ledger, RULES)
    assert res.outcomes.height == 1
    row = res.outcomes.to_dicts()[0]
    assert row["outcome_status"] == "measured"
    assert row["price_before_cents"] == 1_000 and row["price_after_cents"] == 1_100
    assert row["realization_pct"] == pytest.approx(0.5)
    assert row["treated_volume_ratio"] == pytest.approx(1.3 * 0.8, rel=1e-6)
    assert row["control_volume_ratio"] == pytest.approx(1.3, rel=1e-6)
    assert row["control_adjusted_volume_ratio"] == pytest.approx(0.8, rel=1e-6)
    assert row["volume_error_log_points"] == pytest.approx(math.log(0.8), rel=1e-5)
    assert row["n_control_pairs"] == 6
    # Margin outcome present on both sides.
    assert row["pocket_margin_before"] is not None and row["pocket_margin_after"] is not None


def test_insufficient_gates_fail_closed() -> None:
    actions = pl.concat([_action_frame(), _control_actions()], how="vertical")
    events = _events_for(["act_t1"])
    # Too few controls.
    ledger = _ledger(
        treated_after_ratio=0.8, control_trend=1.0, treated_price_after=1_100, n_controls=2
    )
    res = track_outcomes(actions, events, ledger, RULES)
    row = res.outcomes.to_dicts()[0]
    assert row["outcome_status"] == "insufficient_evidence"
    assert "comparison pair" in row["outcome_reason"]
    # Window not elapsed: no after-window data at all.
    short = _ledger(treated_after_ratio=0.8, control_trend=1.0, treated_price_after=1_100).filter(
        pl.col("invoice_date") < pl.lit(IMPL)
    )
    res2 = track_outcomes(actions, events, short, RULES)
    assert "window not elapsed" in res2.outcomes.to_dicts()[0]["outcome_reason"]


def test_calibration_minimum_count_gating() -> None:
    actions = pl.concat([_action_frame(), _control_actions()], how="vertical")
    events = _events_for(["act_t1"])
    ledger = _ledger(treated_after_ratio=0.9, control_trend=1.0, treated_price_after=1_200)
    res = track_outcomes(actions, events, ledger, RULES)  # 1 measured < 10
    cal = res.calibration.to_dicts()
    assert len(cal) == 1
    assert cal[0]["status"] == "not_enough_implemented_actions"
    assert cal[0]["median_abs_volume_error_log_points"] is None


def test_measure_and_record_transitions_once(tmp_path: Path) -> None:
    actions = pl.concat([_action_frame(), _control_actions()], how="vertical")
    events = _events_for(["act_t1"])
    for aid in [f"act_ctl{c}" for c in range(8)]:
        events = pl.concat([events, _events_for([aid], implemented=False).head(1)], how="vertical")
    store = ActionStore(tmp_path / "a.duckdb")
    store.seed(actions, events.sort("action_id", "seq"))
    ledger = _ledger(treated_after_ratio=0.8, control_trend=1.1, treated_price_after=1_150)
    r1 = measure_and_record(store, ledger, RULES, at=AT + dt.timedelta(days=120))
    assert int((r1.outcomes["outcome_status"] == "measured").sum()) == 1
    st = replay_status(store.events())
    assert st.filter(pl.col("action_id") == "act_t1")["status"][0] == "measured"
    n_events = store.events().height
    r2 = measure_and_record(store, ledger, RULES, at=AT + dt.timedelta(days=121))
    assert store.events().height == n_events  # no second measured event
    assert r2.outcomes.height == r1.outcomes.height
    assert store.outcomes().height == r2.outcomes.height


def test_generator_plants_interventions(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(REPO))
    from synth.config import GeneratorConfig, Preset
    from synth.generate import generate

    cfg = GeneratorConfig.for_preset(
        Preset.WELL_IDENTIFIED,
        seed=7,
        n_customers=40,
        n_items=60,
        n_price_interventions=5,
        intervention_pct=0.10,
    )
    generate(cfg, tmp_path / "world")
    gt = tmp_path / "world" / "ground_truth"
    inter = pl.read_parquet(gt / "interventions.parquet")
    assert inter.height == 5
    t0 = int(inter["intervention_month"][0])
    panel = pl.read_parquet(gt / "panel_truth.parquet")
    one = inter.to_dicts()[0]
    pair = panel.filter(
        (pl.col("customer_id") == one["customer_id"]) & (pl.col("item_id") == one["item_id"])
    )
    before = pair.filter(pl.col("month") == t0 - 1)["unit_price_cents"][0]
    after = pair.filter(pl.col("month") == t0)["unit_price_cents"][0]
    # The planted lift lands on the price path at the intervention month
    # (up to the month's own small price noise).
    assert after / before == pytest.approx(1.10, rel=0.05)
    assert not bool(pair["on_contract"][0])
