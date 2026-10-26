"""Action model and append-only event log (Phase 38).

An action's current status is DERIVED by replaying its event log, never edited
in place. Every transition is an event: who, when, from-status, to-status,
note. The transition table is the single source of truth for what is allowed;
Phase 39 adds who may perform each transition (maker-checker).
"""

from __future__ import annotations

import datetime as dt

import polars as pl

# Lifecycle: proposed -> under review -> approved | rejected -> exported ->
# implemented -> measured. A rejected action can be reworked (back to
# proposed); everything else only moves forward.
STATUSES = (
    "proposed",
    "under_review",
    "approved",
    "rejected",
    "exported",
    "implemented",
    "measured",
)

# Statuses where a due date still binds someone to act: an action here with a
# due date before the data's as-of date is overdue. Rejected and implemented/
# measured actions carry no open obligation.
ACTIONABLE_STATUSES = ("proposed", "under_review", "approved", "exported")

ALLOWED_TRANSITIONS: dict[str | None, tuple[str, ...]] = {
    None: ("proposed",),  # birth
    "proposed": ("under_review",),
    "under_review": ("approved", "rejected", "proposed"),
    "rejected": ("proposed",),
    "approved": ("exported",),
    "exported": ("implemented",),
    "implemented": ("measured",),
    "measured": (),
}

ACTION_TYPES = (
    "price_change",
    "below_floor_fix",
    "cost_pass_through",
    "service_policy",
    "rebate_discount_review",
)

EVENTS_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "action_id": pl.String,
    "seq": pl.Int64,
    "who": pl.String,
    "at": pl.Datetime("us", "UTC"),
    "from_status": pl.String,
    "to_status": pl.String,
    "note": pl.String,
}


class TransitionError(ValueError):
    """An event would break the lifecycle (wrong order, unknown status)."""


def empty_events() -> pl.DataFrame:
    return pl.DataFrame(schema=EVENTS_SCHEMA)


def replay_status(events: pl.DataFrame) -> pl.DataFrame:
    """Current status per action, derived from the log: the to_status of the
    highest-seq event. The log is validated on append; replay trusts order."""
    if events.height == 0:
        return pl.DataFrame(schema={"action_id": pl.String, "status": pl.String})
    return (
        events.sort("action_id", "seq")
        .group_by("action_id", maintain_order=True)
        .agg(pl.col("to_status").last().alias("status"))
    )


def validate_log(events: pl.DataFrame) -> None:
    """Every action's event chain must be a valid lifecycle walk: seq dense
    from 0, first event births to 'proposed', every step allowed, and each
    event's from_status equals the previous event's to_status."""
    for (action_id,), grp in events.sort("seq").group_by(["action_id"], maintain_order=True):
        prev: str | None = None
        for i, row in enumerate(grp.iter_rows(named=True)):
            if row["seq"] != i:
                msg = f"action {action_id}: event seq {row['seq']} != expected {i} (gap or dup)"
                raise TransitionError(msg)
            if row["from_status"] != prev:
                msg = (
                    f"action {action_id}: event {i} from_status "
                    f"{row['from_status']!r} != prior status {prev!r}"
                )
                raise TransitionError(msg)
            allowed = ALLOWED_TRANSITIONS.get(prev)
            if allowed is None or row["to_status"] not in allowed:
                msg = (
                    f"action {action_id}: transition {prev!r} -> "
                    f"{row['to_status']!r} is not allowed"
                )
                raise TransitionError(msg)
            prev = row["to_status"]


def append_event(
    events: pl.DataFrame,
    action_id: str,
    *,
    who: str,
    at: dt.datetime,
    to_status: str,
    note: str = "",
) -> pl.DataFrame:
    """Append one validated transition; returns the new log. from_status is
    derived from the log, never passed (the log is the truth)."""
    if to_status not in STATUSES:
        msg = f"unknown status {to_status!r}; expected one of {STATUSES}"
        raise TransitionError(msg)
    mine = events.filter(pl.col("action_id") == action_id).sort("seq")
    current: str | None = mine["to_status"][-1] if mine.height else None
    allowed = ALLOWED_TRANSITIONS.get(current, ())
    if to_status not in allowed:
        msg = (
            f"action {action_id}: cannot move {current!r} -> {to_status!r}; "
            f"allowed next: {list(allowed)}"
        )
        raise TransitionError(msg)
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.UTC)
    row = pl.DataFrame(
        {
            "action_id": [action_id],
            "seq": [mine.height],
            "who": [who],
            "at": [at],
            "from_status": [current],
            "to_status": [to_status],
            "note": [note],
        },
        schema=EVENTS_SCHEMA,
    )
    return pl.concat([events, row], how="vertical")
