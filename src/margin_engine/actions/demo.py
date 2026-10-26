"""Simulated action history and demo realism for the STATIC demo (Phase 38).

The hosted demo is read-only, so it ships a generated action set with a
simulated review history — deterministic (hash of the action id picks the
path), honest (labeled demo data in the UI), and valid (every walk follows the
lifecycle, blocked actions never advance, approvals of compliance-flagged
actions carry a written justification, and the approver is never the
proposer).

Demo realism: owners come from a small roster of named (fictional) people with
roles, proposal dates are staggered per hash bucket, and each due date is the
simulated proposal date plus the priority's lead time — so the queue carries a
realistic mix of upcoming and overdue work relative to the demo's as-of date
(the data's last invoice date). Real runs are untouched: they keep the
configured default owner and anchor-based due dates.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from margin_engine.actions.model import append_event, empty_events
from margin_engine.config import Rules

# Named demo people (fictional) and their roles; owner strings render as
# "Name (role)" so the queue reads like a team's worklist, not placeholders.
DEMO_PEOPLE: tuple[tuple[str, str], ...] = (
    ("Maya Lindqvist", "pricing manager"),
    ("Dan Okafor", "pricing analyst"),
    ("Priya Raman", "sales operations lead"),
    ("Tom Alvarez", "category manager"),
)
DEMO_ANALYST = "Dan Okafor"
DEMO_APPROVER = "Maya Lindqvist"

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

# bucket -> days before the as-of date the action was proposed. Implemented
# walks start early enough for a full 90-day measurement window INSIDE the
# data; proposed-only walks are recent, so due dates mix upcoming and overdue.
_BORN_DAYS: dict[int, int] = {
    0: 5,
    1: 9,
    2: 16,
    3: 21,
    4: 26,
    5: 35,
    6: 40,
    7: 60,
    8: 140,
    9: 150,
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


def apply_demo_realism(actions: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Demo owners and due dates. The owner rotates deterministically over the
    roster; the due date is the simulated proposal date plus the priority's
    lead time (the natural rule a team would use), so some open actions are
    already overdue at the as-of date and others are still upcoming."""
    ac = rules.actions
    due_days = {"high": ac.due_days_high, "medium": ac.due_days_medium, "low": ac.due_days_low}
    rows = []
    for row in actions.iter_rows(named=True):  # action sets are small
        b = _bucket(str(row["action_id"]))
        name, role = DEMO_PEOPLE[b % len(DEMO_PEOPLE)]
        anchor: dt.date = row["created_at"].date()
        born = anchor - dt.timedelta(days=_BORN_DAYS[b])
        row["owner"] = f"{name} ({role})"
        row["due_date"] = born + dt.timedelta(days=due_days.get(str(row["priority"]), 30))
        rows.append(row)
    return pl.DataFrame(rows, schema=actions.schema)


def simulate_demo_history(actions: pl.DataFrame) -> pl.DataFrame:
    """Birth events plus a deterministic partial review history. Returns the
    full event log (validate_log-clean)."""
    events = empty_events()
    for row in actions.sort("action_id").iter_rows(named=True):
        aid = str(row["action_id"])
        born: dt.datetime = row["created_at"]
        if born.tzinfo is None:
            born = born.replace(tzinfo=dt.UTC)
        # Stagger proposals per bucket: implemented walks start >= 140 days
        # before the data's end so their 90-day measurement window closes
        # INSIDE the data (the results view then has real measured outcomes);
        # proposed-only walks are days old, so the queue looks current.
        born = born - dt.timedelta(days=_BORN_DAYS[_bucket(aid)])
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
                at=born + dt.timedelta(days=step * 2),
                to_status=to_status,
                note=note,
            )
    return events
