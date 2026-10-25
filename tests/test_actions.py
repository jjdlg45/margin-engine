"""Phase 38: action model and generation.

- generation is deterministic;
- every action traces to its source records;
- guardrails are enforced (floor, contract protection, volume-evidence
  labeling, Robinson-Patman flag carried);
- status always equals the replayed event log, and invalid walks are refused.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from margin_engine.actions.generate import (
    FloorGuardrailError,
    generate_actions,
)
from margin_engine.actions.model import (
    TransitionError,
    append_event,
    empty_events,
    replay_status,
    validate_log,
)
from margin_engine.actions.store import ActionStore
from margin_engine.config import load_rules
from margin_engine.report.full_run import FullRunResult, run_full

REPO = Path(__file__).resolve().parents[1]
RULES = load_rules(REPO / "config" / "rules.yaml")


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> FullRunResult:
    return run_full(
        REPO / "data" / "sample" / "erp",
        REPO / "config" / "mappings" / "synth_erp.yaml",
        REPO / "config" / "rules.yaml",
        tmp_path_factory.mktemp("out"),
        validate=False,
    )


def _generate(res: FullRunResult) -> pl.DataFrame:
    assert res.service_flags is not None
    out = generate_actions(
        ledger=res.pipeline.ledger,
        guidance=res.guidance,
        opportunities=res.opportunities,
        leakage=res.leakage,
        service_flags=res.service_flags,
        sensitivity=res.sensitivity,
        panel=res.panel,
        rules=RULES,
    )
    return out.actions


def test_generation_is_deterministic(sample_run: FullRunResult) -> None:
    a = _generate(sample_run)
    b = _generate(sample_run)
    assert_frame_equal(a, b)
    assert a.height > 0
    assert a["action_id"].n_unique() == a.height


def test_every_action_traces_to_source_records(sample_run: FullRunResult) -> None:
    actions = sample_run.actions
    assert actions is not None and actions.height > 0
    g_pairs = {
        (r[0], r[1]) for r in sample_run.guidance.select("customer_id", "item_id").iter_rows()
    }
    assert sample_run.service_flags is not None
    flag_customers = set(sample_run.service_flags["customer_id"].to_list())
    leak_customers = set(sample_run.leakage["customer_id"].to_list())
    for r in actions.iter_rows(named=True):
        t = r["action_type"]
        if t in ("price_change", "below_floor_fix"):
            assert (r["customer_id"], r["item_id"]) in g_pairs, r["action_id"]
        elif t == "service_policy":
            assert r["customer_id"] in flag_customers, r["action_id"]
        elif t == "rebate_discount_review":
            assert r["customer_id"] in leak_customers, r["action_id"]
        elif t == "cost_pass_through":
            assert r["scope"].split(":", 1)[0] in ("category", "supplier")
    # below_floor actions exist iff guidance says below_floor, one per pair
    bf_guidance = sample_run.guidance.filter(pl.col("guidance_status") == "below_floor")
    bf_actions = actions.filter(pl.col("action_type") == "below_floor_fix")
    assert bf_actions.height == min(bf_guidance.height, RULES.actions.max_actions_per_type)


def test_volume_evidence_labeling(sample_run: FullRunResult) -> None:
    actions = sample_run.actions
    assert actions is not None
    pc = actions.filter(pl.col("action_type") == "price_change")
    expected = pc.filter(pl.col("impact_basis") == "expected_contribution_change")
    assert (expected["volume_treatment"] == "expected_change").all()
    fixed = pc.filter(pl.col("impact_basis") != "expected_contribution_change")
    assert (fixed["volume_treatment"] != "expected_change").all()


def test_rp_flag_travels_with_actions(sample_run: FullRunResult) -> None:
    actions = sample_run.actions
    assert actions is not None
    flagged_pairs = {
        (r[0], r[1])
        for r in sample_run.guidance.filter(pl.col("rp_review_flag"))
        .select("customer_id", "item_id")
        .iter_rows()
    }
    if not flagged_pairs:
        pytest.skip("sample world produced no RP flags")
    pair_actions = actions.filter(pl.col("action_type").is_in(["price_change", "below_floor_fix"]))
    for r in pair_actions.iter_rows(named=True):
        want = "robinson_patman" if (r["customer_id"], r["item_id"]) in flagged_pairs else ""
        assert r["compliance_flags"] == want


def _mini_inputs(
    *, floor_index: float, proposed: int, status: str, protected: bool
) -> dict[str, pl.DataFrame]:
    guidance = pl.DataFrame(
        {
            "customer_id": ["C1"],
            "item_id": ["I1"],
            "segment_leaf": [1],
            "guidance_status": [status],
            "contract_protected": [protected],
            "rp_review_flag": [False],
            "floor_index": [floor_index],
            "eff_list_cents": [10_000],
            "binding_floor_basis": ["pocket_gross_margin"],
        }
    )
    opportunities = pl.DataFrame(
        {
            "customer_id": ["C1"],
            "item_id": ["I1"],
            "baseline_qty": [10.0],
            "current_invoice_price_cents": [9_000.0],
            "proposed_invoice_price_cents": [float(proposed)],
            "gross_price_opportunity_cents": [1_000],
            "expected_contribution_change_cents": [None],
            "volume_treatment": ["fixed_volume_only"],
            "evidence_status": ["descriptive_elasticity"],
            "pocket_retention": [1.0],
        }
    )
    ledger = pl.DataFrame(
        {
            "invoice_date": [dt.date(2026, 1, 1)],
            "transaction_type": ["sale"],
            "item_id": ["I1"],
            "net_amount_cents": [90_000],
        }
    )
    leakage = pl.DataFrame(
        schema={
            "customer_id": pl.String,
            "net_revenue_cents": pl.Int64,
            "total_leakage_cents": pl.Int64,
        }
    )
    sensitivity = pl.DataFrame()
    return {
        "ledger": ledger,
        "guidance": guidance,
        "opportunities": opportunities,
        "leakage": leakage,
        "sensitivity": sensitivity,
    }


def test_floor_guardrail_raises_on_violation() -> None:
    # floor index 1.1 x list $100 = $110 floor; a proposed $105 must refuse.
    inputs = _mini_inputs(
        floor_index=1.1, proposed=10_500, status="below_reference", protected=False
    )
    with pytest.raises(FloorGuardrailError):
        generate_actions(service_flags=None, panel=None, rules=RULES, **inputs)


def test_contract_protected_actions_blocked_with_reason() -> None:
    inputs = _mini_inputs(floor_index=1.2, proposed=11_000, status="below_floor", protected=True)
    out = generate_actions(service_flags=None, panel=None, rules=RULES, **inputs)
    bf = out.actions.filter(pl.col("action_type") == "below_floor_fix")
    assert bf.height == 1
    row = bf.to_dicts()[0]
    assert row["blocked"] is True
    assert "contract-protected" in row["blocked_reason"]
    # And the protected pair never becomes a price_change action.
    assert out.actions.filter(pl.col("action_type") == "price_change").height == 0


def test_lifecycle_replay_and_invalid_transitions() -> None:
    at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    log = empty_events()
    log = append_event(log, "a1", who="engine", at=at, to_status="proposed")
    log = append_event(log, "a1", who="analyst", at=at, to_status="under_review")
    log = append_event(log, "a1", who="approver", at=at, to_status="approved")
    log = append_event(log, "a1", who="system", at=at, to_status="exported")
    log = append_event(log, "a1", who="analyst", at=at, to_status="implemented")
    log = append_event(log, "a1", who="engine", at=at, to_status="measured")
    status = replay_status(log)
    assert status.to_dicts() == [{"action_id": "a1", "status": "measured"}]
    validate_log(log)
    # Skipping review is refused; so is moving a measured action.
    log2 = append_event(empty_events(), "a2", who="engine", at=at, to_status="proposed")
    with pytest.raises(TransitionError):
        append_event(log2, "a2", who="x", at=at, to_status="approved")
    with pytest.raises(TransitionError):
        append_event(log, "a1", who="x", at=at, to_status="proposed")
    # A tampered log (edited in place) fails validation.
    tampered = log.with_columns(
        pl.when(pl.col("seq") == 2)
        .then(pl.lit("rejected"))
        .otherwise(pl.col("to_status"))
        .alias("to_status")
    )
    with pytest.raises(TransitionError):
        validate_log(tampered)


def test_store_seed_idempotent_and_transitions(tmp_path: Path) -> None:
    at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    inputs = _mini_inputs(
        floor_index=0.5, proposed=9_500, status="below_reference", protected=False
    )
    out = generate_actions(service_flags=None, panel=None, rules=RULES, **inputs)
    assert out.actions.height == 1
    aid = out.actions["action_id"][0]

    store = ActionStore(tmp_path / "actions.duckdb")
    assert store.seed(out.actions, out.events) is True
    assert store.seed(out.actions, out.events) is False  # already seeded: no-op
    store.transition(aid, who="analyst", to_status="under_review", at=at)
    store.transition(aid, who="approver", to_status="approved", at=at)
    events = store.events()
    validate_log(events)
    status = replay_status(events)
    assert status.filter(pl.col("action_id") == aid)["status"][0] == "approved"
    # The lifecycle is enforced at the store boundary too.
    with pytest.raises(TransitionError):
        store.transition(aid, who="x", to_status="measured", at=at)


def test_demo_history_is_deterministic_and_valid(sample_run: FullRunResult) -> None:
    from margin_engine.actions.demo import simulate_demo_history

    actions = sample_run.actions
    assert actions is not None and actions.height > 0
    h1 = simulate_demo_history(actions)
    h2 = simulate_demo_history(actions)
    assert_frame_equal(h1, h2)
    validate_log(h1)
    status = replay_status(h1)
    # Blocked actions never advance past proposed.
    blocked_ids = actions.filter(pl.col("blocked"))["action_id"].to_list()
    if blocked_ids:
        st = status.filter(pl.col("action_id").is_in(blocked_ids))
        assert (st["status"] == "proposed").all()
    # Every approval of a compliance-flagged action carries a justification.
    flagged_ids = set(actions.filter(pl.col("compliance_flags") != "")["action_id"].to_list())
    approvals = h1.filter(
        (pl.col("to_status") == "approved") & pl.col("action_id").is_in(sorted(flagged_ids))
    )
    if approvals.height:
        assert (approvals["note"].str.len_chars() > 0).all()
    # The simulated reviewer and approver are distinct people.
    assert h1.filter(pl.col("to_status") == "approved")["who"].unique().to_list() in (
        [],
        ["Demo approver"],
    )


def test_store_migrates_older_schema(tmp_path: Path) -> None:
    """A store written before a schema column existed (e.g. uom, Phase 40)
    reads forward: missing columns come back as nulls, never a crash."""
    from margin_engine.actions.generate import ACTIONS_SCHEMA

    inputs = _mini_inputs(
        floor_index=0.5, proposed=9_500, status="below_reference", protected=False
    )
    out = generate_actions(service_flags=None, panel=None, rules=RULES, **inputs)
    store = ActionStore(tmp_path / "old.duckdb")
    store.seed(out.actions.drop("uom"), out.events)  # simulate the older store
    migrated = store.actions()
    assert list(migrated.columns) == list(ACTIONS_SCHEMA)
    assert migrated["uom"].is_null().all()
    # merge_new over the migrated store works too.
    assert store.merge_new(out.actions, out.events) == 0


def test_merge_new_into_older_schema_store(tmp_path: Path) -> None:
    inputs = _mini_inputs(
        floor_index=0.5, proposed=9_500, status="below_reference", protected=False
    )
    out = generate_actions(service_flags=None, panel=None, rules=RULES, **inputs)
    second = _mini_inputs(
        floor_index=0.5, proposed=9_600, status="below_reference", protected=False
    )
    second["guidance"] = second["guidance"].with_columns(pl.lit("I2").alias("item_id"))
    second["opportunities"] = second["opportunities"].with_columns(pl.lit("I2").alias("item_id"))
    out2 = generate_actions(service_flags=None, panel=None, rules=RULES, **second)
    store = ActionStore(tmp_path / "old2.duckdb")
    store.seed(out.actions.drop("uom"), out.events)  # older schema on disk
    added = store.merge_new(out2.actions, out2.events)  # current schema incoming
    assert added == 1
    merged = store.actions()
    assert merged.height == 2
    from margin_engine.actions.generate import ACTIONS_SCHEMA

    assert list(merged.columns) == list(ACTIONS_SCHEMA)
