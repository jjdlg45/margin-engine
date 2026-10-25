"""Simulated action history for the STATIC demo (Phase 38).

The hosted demo is read-only, so it ships a generated action set with a
simulated review history — deterministic (hash of the action id picks the
path), honest (labeled demo data in the UI), and valid (every walk follows the
lifecycle, blocked actions never advance, approvals of compliance-flagged
actions carry a written justification, and the approver is never the
proposer).
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from margin_engine.actions.model import append_event, empty_events

DEMO_ANALYST = "Demo analyst"
DEMO_APPROVER = "Demo approver"

# hash bucket (0-9) -> how far the demo walk goes.
_PATHS: dict[int, tuple[str, ...]] = {
    0: (),
    1: (),
    2: (),
    3: ("under_review",),
    4: ("under_review",),
    5: ("under_review", "rejected"),
    6: ("under_review", "approved"),
    7: ("under_review", "approved", "exported"),
    8: ("under_review", "approved", "exported", "implemented"),
    9: ("under_review", "approved", "exported", "implemented"),
}

_WHO = {
    "under_review": DEMO_ANALYST,
    "approved": DEMO_APPROVER,
    "rejected": DEMO_APPROVER,
    "exported": DEMO_ANALYST,
    "implemented": DEMO_ANALYST,
}


def _bucket(action_id: str) -> int:
    # The id is already a content hash (act_<12 hex>): its last hex digit is a
    # uniform, deterministic bucket.
    return int(action_id[-1], 16) % 10


def simulate_demo_history(actions: pl.DataFrame) -> pl.DataFrame:
    """Birth events plus a deterministic partial review history. Returns the
    full event log (validate_log-clean)."""
    events = empty_events()
    for row in actions.sort("action_id").iter_rows(named=True):
        aid = str(row["action_id"])
        born: dt.datetime = row["created_at"]
        if born.tzinfo is None:
            born = born.replace(tzinfo=dt.UTC)
        # The simulated review starts ~140 days before the data's end so that
        # implemented actions have a full 90-day measurement window INSIDE the
        # data - the demo's results view then has real measured outcomes.
        born = born - dt.timedelta(days=140)
        events = append_event(
            events, aid, who="engine", at=born, to_status="proposed", note="generated (demo data)"
        )
        if row["blocked"]:
            continue  # a blocked action never advances
        for step, to_status in enumerate(_PATHS[_bucket(aid)], start=1):
            note = ""
            if to_status == "approved" and str(row["compliance_flags"] or ""):
                note = (
                    "Justification (demo): price difference reflects the "
                    "cost-to-serve difference shown on the compliance detail; "
                    "reviewed against the Robinson-Patman checklist."
                )
            elif to_status == "rejected":
                note = "Rejected (demo): owner asked for a smaller first step."
            events = append_event(
                events,
                aid,
                who=_WHO[to_status],
                at=born + dt.timedelta(days=step),
                to_status=to_status,
                note=note,
            )
    return events
