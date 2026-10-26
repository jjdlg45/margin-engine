"""Phase 69 (Run 6): stale actions, safe transitions, export recovery.

Findings 19 and 20. Three independent guarantees under test:
1. a proposal is revalidated against the engine's CURRENT outputs at approval
   and export; what moved is shown, both versions are audited, and a blocking
   verdict refuses rather than letting a stale number through;
2. two simultaneous transitions cannot both win - the loser writes nothing;
3. a crash between writing the price file and updating statuses recovers
   idempotently, and never re-exports a superseded or implemented price.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from margin_engine.actions.export import (
    build_price_file,
    export_price_file,
    recover_pending_exports,
)
from margin_engine.actions.generate import ACTIONS_SCHEMA
from margin_engine.actions.model import replay_status
from margin_engine.actions.revalidate import (
    VERDICT_INVALID,
    VERDICT_NOT_TESTABLE,
    VERDICT_RE_REVIEW,
    VERDICT_UNCHANGED,
    VERDICT_WITHIN_TOLERANCE,
    revalidate_action,
    revalidation_rows,
)
from margin_engine.actions.store import ActionStore, ConcurrencyError
from margin_engine.config import Rules

AT = dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 1, 10)


def _action(**kw: object) -> dict[str, object]:
    base: dict[str, object] = dict.fromkeys(ACTIONS_SCHEMA)
    base.update(
        {
            "action_id": "act_stale0001",
            "action_type": "price_change",
            "title": "Raise price",
            "rationale": "r",
            "customer_id": "C1",
            "item_id": "I1",
            "scope": "customer_item",
            "uom": "EA",
            "guidance_status": "below_reference",
            "evidence_status": "descriptive_elasticity",
            "volume_treatment": "fixed_volume_only",
            "impact_basis": "gross_price_opportunity_fixed_volume",
            "expected_impact_cents": 10_000,
            "current_price_cents": 9_000,
            "proposed_price_cents": 9_500,
            "floor_price_cents": 6_316,
            "scope_key": "price|C1|I1|EA|2026-02-01",
            "priority": "medium",
            "owner": "unassigned",
            "compliance_flags": "",
            "blocked": False,
            "expires_on": dt.date(2026, 4, 1),
            "created_at": AT,
        }
    )
    base.update(kw)
    return base


def _guidance(**kw: object) -> pl.DataFrame:
    base: dict[str, object] = {
        "customer_id": "C1",
        "item_id": "I1",
        "guidance_status": "below_reference",
        "current_index": 0.90,
        "eff_list_cents": 10_000,
        "floor_index": 0.6316,
        "invoice_floor_cents": 6_316,
        "list_provenance": "effective_list",
        "contract_protected": False,
        "rp_review_flag": False,
    }
    base.update(kw)
    return pl.DataFrame([base])


# ---------------------------------------------------------------------------
# Revalidation verdicts
# ---------------------------------------------------------------------------


def test_unchanged_world_passes_cleanly() -> None:
    rev = revalidate_action(_action(), rules=Rules(), guidance=_guidance(), as_of=TODAY)
    assert rev.verdict == VERDICT_UNCHANGED
    assert not rev.blocking
    assert "nothing material moved" in rev.message()


def test_absent_guidance_is_not_testable_never_an_assumed_pass() -> None:
    rev = revalidate_action(_action(), rules=Rules(), guidance=None, as_of=TODAY)
    assert rev.verdict == VERDICT_NOT_TESTABLE
    assert "could not be revalidated" in rev.message()


def test_floor_move_beyond_threshold_requires_re_review() -> None:
    # Floor moved 6,316 -> 6,800 cents (7.7%), far beyond the 2% default.
    rev = revalidate_action(
        _action(),
        rules=Rules(),
        guidance=_guidance(floor_index=0.68, invoice_floor_cents=6_800),
        as_of=TODAY,
    )
    assert rev.verdict == VERDICT_RE_REVIEW
    assert rev.blocking
    assert any(d.field == "binding floor" for d in rev.differences)
    assert "7.7%" in rev.message()


def test_small_floor_move_stays_within_tolerance() -> None:
    # 6,316 -> 6,380 is 1.0%, inside the 2% threshold: reported, not blocking.
    rev = revalidate_action(
        _action(),
        rules=Rules(),
        guidance=_guidance(floor_index=0.638, invoice_floor_cents=6_380),
        as_of=TODAY,
    )
    assert rev.verdict == VERDICT_WITHIN_TOLERANCE
    assert not rev.blocking
    assert rev.differences  # the move is still shown


def test_proposed_price_now_below_floor_is_invalid() -> None:
    rev = revalidate_action(
        _action(),
        rules=Rules(),
        guidance=_guidance(floor_index=0.99, invoice_floor_cents=9_900),
        as_of=TODAY,
    )
    assert rev.verdict == VERDICT_INVALID
    assert any(d.field == "proposed price vs floor" for d in rev.differences)


def test_contract_protection_appearing_is_invalid() -> None:
    rev = revalidate_action(
        _action(), rules=Rules(), guidance=_guidance(contract_protected=True), as_of=TODAY
    )
    assert rev.verdict == VERDICT_INVALID
    assert any(d.field == "contract protection" for d in rev.differences)


def test_guidance_gone_insufficient_is_invalid() -> None:
    rev = revalidate_action(
        _action(),
        rules=Rules(),
        guidance=_guidance(guidance_status="insufficient_data"),
        as_of=TODAY,
    )
    assert rev.verdict == VERDICT_INVALID


def test_pair_vanished_from_guidance_is_invalid() -> None:
    rev = revalidate_action(
        _action(customer_id="C9"), rules=Rules(), guidance=_guidance(), as_of=TODAY
    )
    assert rev.verdict == VERDICT_INVALID
    assert any(d.field == "pair" for d in rev.differences)


def test_current_price_move_requires_re_review() -> None:
    # The pair's realized price moved 9,000 -> 9,400 (4.4%): the gap the
    # proposal was sized on no longer holds.
    rev = revalidate_action(
        _action(), rules=Rules(), guidance=_guidance(current_index=0.94), as_of=TODAY
    )
    assert rev.verdict == VERDICT_RE_REVIEW
    assert any(d.field == "current price" for d in rev.differences)


def test_stale_list_provenance_requires_re_review() -> None:
    rev = revalidate_action(
        _action(), rules=Rules(), guidance=_guidance(list_provenance="stale_list"), as_of=TODAY
    )
    assert rev.blocking
    assert any(d.field == "list price provenance" for d in rev.differences)


def test_new_compliance_flag_requires_re_review() -> None:
    rev = revalidate_action(
        _action(), rules=Rules(), guidance=_guidance(rp_review_flag=True), as_of=TODAY
    )
    assert rev.verdict == VERDICT_RE_REVIEW
    assert any(d.field == "compliance flag" for d in rev.differences)


def test_expired_proposal_requires_re_review() -> None:
    rev = revalidate_action(
        _action(expires_on=dt.date(2026, 1, 1)),
        rules=Rules(),
        guidance=_guidance(),
        as_of=TODAY,
    )
    assert rev.blocking
    assert any(d.field == "expiry" for d in rev.differences)


def test_scope_collision_is_invalid() -> None:
    rev = revalidate_action(
        _action(),
        rules=Rules(),
        guidance=_guidance(),
        as_of=TODAY,
        active_scope_keys={"price|C1|I1|EA|2026-02-01"},
    )
    assert rev.verdict == VERDICT_INVALID
    assert any(d.field == "scope" for d in rev.differences)


def test_untestable_checks_are_named_never_assumed_clean() -> None:
    """The web bundle's guidance table omits contract protection and the
    compliance flag; those checks must be NAMED as not revalidated."""
    slim = pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
                "status": "below_reference",
                "current_index": 0.90,
                "floor_index": 0.6316,
                "invoice_floor_cents": 6_316,
                "list_provenance": "effective_list",
            }
        ]
    )
    rev = revalidate_action(_action(), rules=Rules(), guidance=slim, as_of=TODAY)
    assert "contract protection" in rev.not_testable
    assert "compliance flag" in rev.not_testable
    assert "not revalidated" in rev.message()


def test_revalidation_disabled_short_circuits() -> None:
    rules = Rules.model_validate({"actions": {"revalidation": {"enabled": False}}})
    rev = revalidate_action(
        _action(), rules=rules, guidance=_guidance(contract_protected=True), as_of=TODAY
    )
    assert rev.verdict == VERDICT_UNCHANGED
    assert not rev.blocking


def test_audit_rows_keep_both_versions() -> None:
    rev = revalidate_action(
        _action(),
        rules=Rules(),
        guidance=_guidance(floor_index=0.68, invoice_floor_cents=6_800),
        as_of=TODAY,
    )
    rows = revalidation_rows("act_stale0001", rev, who="bob", trigger="approval", at=AT)
    assert rows.height == len(rev.differences)
    r = rows.to_dicts()[0]
    assert r["stored_value"] == "6316 cents"
    assert r["current_value"] == "6800 cents"
    assert r["verdict"] == VERDICT_RE_REVIEW
    assert r["trigger"] == "approval"
    assert r["who"] == "bob"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _seeded_store(tmp_path: Path) -> ActionStore:
    store = ActionStore(tmp_path / "actions.duckdb")
    actions = pl.DataFrame([_action()], schema=ACTIONS_SCHEMA)
    events = pl.DataFrame(
        {
            "action_id": ["act_stale0001"],
            "seq": [0],
            "who": ["engine"],
            "at": [AT],
            "from_status": [None],
            "to_status": ["proposed"],
            "note": ["generated"],
        },
        schema={
            "action_id": pl.String,
            "seq": pl.Int64,
            "who": pl.String,
            "at": pl.Datetime("us", "UTC"),
            "from_status": pl.String,
            "to_status": pl.String,
            "note": pl.String,
        },
    )
    store.seed(actions, events, replace=True)
    return store


def test_expected_sequence_mismatch_writes_nothing(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    # The log is at sequence 1 (one birth event). A caller that believes it is
    # still at 0 lost a race: it must write nothing.
    with pytest.raises(ConcurrencyError, match="expected event sequence 0"):
        store.transition(
            "act_stale0001", who="ann", to_status="under_review", at=AT, expected_seq=0
        )
    assert store.events().height == 1  # untouched
    # The correct token succeeds.
    store.transition("act_stale0001", who="ann", to_status="under_review", at=AT, expected_seq=1)
    assert store.events().height == 2
    assert replay_status(store.events())["status"][0] == "under_review"


def test_second_simultaneous_transition_loses(tmp_path: Path) -> None:
    """Both callers read the log at sequence 1, then both submit. The first
    wins; the second is refused and nothing is written twice."""
    store = _seeded_store(tmp_path)
    seen = store.events().height  # both callers' view
    store.transition("act_stale0001", who="ann", to_status="under_review", at=AT, expected_seq=seen)
    with pytest.raises(ConcurrencyError):
        store.transition("act_stale0001", who="bob", to_status="rejected", at=AT, expected_seq=seen)
    assert store.events().height == 2
    assert replay_status(store.events())["status"][0] == "under_review"


def test_omitted_token_keeps_working(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    store.transition("act_stale0001", who="ann", to_status="under_review", at=AT)
    assert store.events().height == 2


# ---------------------------------------------------------------------------
# Export recovery
# ---------------------------------------------------------------------------


def _approved_store(tmp_path: Path, n: int = 2) -> ActionStore:
    store = ActionStore(tmp_path / "actions.duckdb")
    rows = [
        _action(
            action_id=f"act_exp{i:08d}",
            item_id=f"I{i}",
            scope_key=f"price|C1|I{i}|EA|2026-02-01",
        )
        for i in range(n)
    ]
    actions = pl.DataFrame(rows, schema=ACTIONS_SCHEMA)
    ev: list[dict[str, object]] = []
    for i in range(n):
        aid = f"act_exp{i:08d}"
        for seq, (frm, to) in enumerate(
            [(None, "proposed"), ("proposed", "under_review"), ("under_review", "approved")]
        ):
            ev.append(
                {
                    "action_id": aid,
                    "seq": seq,
                    "who": "engine" if seq == 0 else "ann",
                    "at": AT,
                    "from_status": frm,
                    "to_status": to,
                    "note": "",
                }
            )
    events = pl.DataFrame(ev).cast(
        {
            "action_id": pl.String,
            "seq": pl.Int64,
            "who": pl.String,
            "at": pl.Datetime("us", "UTC"),
            "from_status": pl.String,
            "to_status": pl.String,
            "note": pl.String,
        }
    )
    store.seed(actions, events, replace=True)
    return store


def test_interrupted_export_recovers_idempotently(tmp_path: Path) -> None:
    """Simulate a crash between the file write and the status updates: journal
    an export, do not transition, then run the next export. Recovery completes
    exactly those transitions and commits the journal; running it again is a
    no-op."""
    store = _approved_store(tmp_path)
    ids = sorted(store.actions()["action_id"].to_list())
    store.journal_export("exp_crashed01", who="ann", path="prices.csv", action_ids=ids, at=AT)
    assert not bool(store.export_journal()["committed"][0])

    recovered = recover_pending_exports(store, who="ann", at=AT)
    assert recovered == len(ids)
    status = dict(replay_status(store.events()).select("action_id", "status").iter_rows())
    assert set(status.values()) == {"exported"}
    assert bool(store.export_journal()["committed"][0])
    # Idempotent: a second recovery changes nothing.
    assert recover_pending_exports(store, who="ann", at=AT) == 0


def test_recovery_never_reintroduces_an_implemented_price(tmp_path: Path) -> None:
    """An action that moved on to implemented must not be re-exported by a
    recovery pass that still names it."""
    store = _approved_store(tmp_path, n=1)
    aid = store.actions()["action_id"][0]
    store.transition(aid, who="ann", to_status="exported", at=AT)
    store.transition(aid, who="ann", to_status="implemented", at=AT)
    store.journal_export("exp_stale01", who="ann", path="prices.csv", action_ids=[aid], at=AT)
    assert recover_pending_exports(store, who="ann", at=AT) == 0
    assert replay_status(store.events())["status"][0] == "implemented"
    assert bool(store.export_journal().filter(pl.col("export_id") == "exp_stale01")["committed"][0])


def test_export_journals_before_writing_and_commits_after(tmp_path: Path) -> None:
    store = _approved_store(tmp_path)
    out = tmp_path / "prices.csv"
    result = export_price_file(store, Rules(), out, who="ann", at=AT)
    assert result.n_transitioned == 2
    journal = store.export_journal()
    assert journal.height == 1
    assert bool(journal["committed"][0])
    assert journal["path"][0] == str(out)
    assert out.exists()
    # Re-exporting writes the identical file and logs no new transition.
    again = export_price_file(store, Rules(), out, who="ann", at=AT)
    assert again.n_transitioned == 0
    assert again.csv_text == result.csv_text


def test_export_holds_out_stale_rows(tmp_path: Path) -> None:
    """With current guidance supplied, a row whose floor now exceeds its
    proposed price is HELD OUT of the file with its reason, not exported on
    stale evidence."""
    store = _approved_store(tmp_path, n=2)
    guidance = pl.concat(
        [
            _guidance(item_id="I0"),
            _guidance(item_id="I1", floor_index=0.99, invoice_floor_cents=9_900),
        ]
    )
    out = tmp_path / "prices.csv"
    result = export_price_file(store, Rules(), out, who="ann", at=AT, guidance=guidance)
    held = list(result.summary["held_out_stale"])  # type: ignore[call-overload]
    assert len(held) == 1
    assert held[0]["action_id"] == "act_exp00000001"
    assert "below the binding floor" in held[0]["reason"]
    # Only the clean row reached the file and the exported status.
    assert "act_exp00000000" in result.csv_text
    assert "act_exp00000001" not in result.csv_text
    status = dict(replay_status(store.events()).select("action_id", "status").iter_rows())
    assert status["act_exp00000000"] == "exported"
    assert status["act_exp00000001"] == "approved"
    # The held-out row's revalidation landed in the audit trail.
    audit = store.revalidations("act_exp00000001")
    assert audit.height >= 1
    assert audit["trigger"][0] == "export"


def test_generated_actions_carry_fingerprints_and_expiry() -> None:
    """The generator stamps the fingerprints and expiry every proposal rests
    on (checked structurally so the schema cannot drift silently)."""
    for col in ("input_fingerprint", "config_fingerprint", "model_fingerprint", "expires_on"):
        assert col in ACTIONS_SCHEMA


def test_price_file_excludes_non_active_statuses(tmp_path: Path) -> None:
    store = _approved_store(tmp_path, n=1)
    aid = store.actions()["action_id"][0]
    store.transition(aid, who="ann", to_status="exported", at=AT)
    store.transition(aid, who="ann", to_status="implemented", at=AT)
    csv_text, rows = build_price_file(store.actions(), store.events(), Rules().actions.price_export)
    assert rows.height == 0
    assert aid not in csv_text
