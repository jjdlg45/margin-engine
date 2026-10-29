"""Stale-action revalidation (Run 6, Phase 69 / Findings 19, 20).

A proposal is a claim about the world at generation time. Between proposal and
approval the engine may have run again: costs move, floors move, a contract
starts protecting the line, a compliance flag appears, another action takes the
same scope. This module compares an action's STORED key values against the
engine's CURRENT outputs and returns a verdict:

- `unchanged`            nothing material moved
- `changed_within_tolerance`  something moved, inside the configured thresholds
- `re_review_required`   a key value moved beyond its threshold, or the
                         proposal expired — the action goes back for review
- `invalid`              the action can no longer be approved at all: the line
                         is now contract-protected, its guidance is
                         insufficient, another active price covers its scope,
                         or the proposed price now sits below the binding floor

Nothing is auto-corrected. The differences are returned field by field, both
versions are written to the audit trail, and a human decides. Approval and
export call this; when the engine's current outputs are unavailable the verdict
is `not_testable` and that is stated rather than assumed clean.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from margin_engine.config import Rules

VERDICT_UNCHANGED = "unchanged"
VERDICT_WITHIN_TOLERANCE = "changed_within_tolerance"
VERDICT_RE_REVIEW = "re_review_required"
VERDICT_INVALID = "invalid"
VERDICT_NOT_TESTABLE = "not_testable"

BLOCKING_VERDICTS = (VERDICT_RE_REVIEW, VERDICT_INVALID)

REVALIDATION_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "action_id": pl.String,
    "seq": pl.Int64,
    "at": pl.Datetime("us", "UTC"),
    "who": pl.String,
    "trigger": pl.String,  # approval | export
    "verdict": pl.String,
    "field": pl.String,
    "stored_value": pl.String,
    "current_value": pl.String,
    "reason": pl.String,
}


@dataclass
class Difference:
    field: str
    stored: object
    current: object
    reason: str
    blocking: bool = False


@dataclass
class Revalidation:
    verdict: str
    differences: list[Difference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Checks the available data could not support (Run 6, Phase 69). A check
    # that could not run is NEVER reported as a pass: it is named here and in
    # the reviewer's message.
    not_testable: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.verdict in BLOCKING_VERDICTS

    def message(self) -> str:
        """One plain-English line per difference, for the reviewer."""
        if self.verdict == VERDICT_NOT_TESTABLE:
            return (
                "the engine's current guidance is not available, so this "
                "proposal's inputs could not be revalidated"
            )
        parts = [
            f"{d.field}: was {d.stored}, now {d.current} ({d.reason})" for d in self.differences
        ]
        if not parts:
            parts.append("nothing material moved since this action was proposed")
        if self.not_testable:
            parts.append(
                "not revalidated (the current outputs do not carry it): "
                + ", ".join(self.not_testable)
            )
        return "; ".join(parts)


def _rel_move(stored: object, current: object) -> float | None:
    try:
        s, c = float(stored), float(current)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if s == 0:
        return None if c == 0 else float("inf")
    return abs(c - s) / abs(s)


def revalidate_action(
    action: dict[str, Any],
    *,
    rules: Rules,
    guidance: pl.DataFrame | None,
    as_of: dt.date | None = None,
    active_scope_keys: set[str] | None = None,
) -> Revalidation:
    """Compare an action's stored key values to current guidance.

    `guidance` is the engine's latest guidance frame (None → not testable).
    `active_scope_keys` names scopes already covered by an active price action
    other than this one; a collision is invalid, not a tolerance question.
    """
    rc = rules.actions.revalidation
    if not rc.enabled:
        return Revalidation(verdict=VERDICT_UNCHANGED, notes=["revalidation disabled in config"])

    diffs: list[Difference] = []
    notes: list[str] = []

    # Expiry is testable without any engine output.
    expires = action.get("expires_on")
    if isinstance(expires, dt.date) and as_of is not None and as_of > expires:
        diffs.append(
            Difference(
                field="expiry",
                stored=str(expires),
                current=str(as_of),
                reason="the proposal expired; its evidence is older than the policy window",
                blocking=True,
            )
        )

    scope = action.get("scope_key")
    if scope and active_scope_keys and str(scope) in active_scope_keys:
        diffs.append(
            Difference(
                field="scope",
                stored=str(scope),
                current="already covered by another active price action",
                reason="one active proposed price per customer, item, unit and effective date",
                blocking=True,
            )
        )

    is_price = str(action.get("action_type")) in ("price_change", "below_floor_fix")
    cust, item = action.get("customer_id"), action.get("item_id")
    if guidance is None:
        verdict = VERDICT_NOT_TESTABLE if not diffs else VERDICT_RE_REVIEW
        return Revalidation(verdict=verdict, differences=diffs, notes=notes)
    if not (is_price and cust and item):
        # Non-price actions carry no pair-level key values to revalidate here;
        # expiry and scope still applied above.
        verdict = VERDICT_RE_REVIEW if any(d.blocking for d in diffs) else VERDICT_UNCHANGED
        return Revalidation(verdict=verdict, differences=diffs, notes=notes)

    row = guidance.filter(
        (pl.col("customer_id").cast(pl.String) == str(cust))
        & (pl.col("item_id").cast(pl.String) == str(item))
    )
    if row.height == 0:
        diffs.append(
            Difference(
                field="pair",
                stored="present in guidance at proposal time",
                current="absent from current guidance",
                reason="the pair no longer appears in the engine's guidance output",
                blocking=True,
            )
        )
        return Revalidation(verdict=VERDICT_INVALID, differences=diffs, notes=notes)
    g = row.to_dicts()[0]
    not_testable: list[str] = []
    # The web bundle's guidance table names the status column `status` and
    # omits some engine-only columns; accept either shape and NAME whatever
    # cannot be checked rather than treating it as clean.
    if "guidance_status" not in g and "status" in g:
        g["guidance_status"] = g["status"]
    for col, label in (
        ("contract_protected", "contract protection"),
        ("rp_review_flag", "compliance flag"),
        ("eff_list_cents", "current price (needs the effective list price)"),
    ):
        if col not in g:
            not_testable.append(label)

    # --- hard invalidations ---------------------------------------------------
    if "contract_protected" in g and bool(g.get("contract_protected")):
        diffs.append(
            Difference(
                field="contract protection",
                stored="not protected",
                current="contract-protected",
                reason="price actions are blocked while price protection applies",
                blocking=True,
            )
        )
    if str(g.get("guidance_status")) == "insufficient_data":
        diffs.append(
            Difference(
                field="guidance status",
                stored=str(action.get("guidance_status")),
                current="insufficient data",
                reason="the pair's guidance no longer supports a recommendation",
                blocking=True,
            )
        )

    # --- current floor --------------------------------------------------------
    floor_now: int | None = None
    if g.get("floor_index") is not None and g.get("eff_list_cents"):
        floor_now = round(float(g["floor_index"]) * float(g["eff_list_cents"]))
    elif g.get("invoice_floor_cents") is not None:
        floor_now = int(g["invoice_floor_cents"])
    stored_floor = action.get("floor_price_cents")
    proposed = action.get("proposed_price_cents")
    if floor_now is not None and proposed is not None and int(proposed) < floor_now:
        diffs.append(
            Difference(
                field="proposed price vs floor",
                stored=f"{proposed} cents",
                current=f"floor is now {floor_now} cents",
                reason="the proposed price now sits below the binding floor",
                blocking=True,
            )
        )
    if floor_now is not None and stored_floor is not None and int(stored_floor) != floor_now:
        move = _rel_move(stored_floor, floor_now)
        beyond = move is not None and move > rc.floor_change_pct
        # Every move is SHOWN; only a move beyond the threshold blocks.
        diffs.append(
            Difference(
                field="binding floor",
                stored=f"{stored_floor} cents",
                current=f"{floor_now} cents",
                reason=(
                    f"the floor moved {move:.1%}, beyond the {rc.floor_change_pct:.0%} "
                    "re-review threshold"
                    if beyond
                    else f"the floor moved {move:.1%}, inside the "
                    f"{rc.floor_change_pct:.0%} threshold"
                )
                if move is not None
                else "the floor changed",
                blocking=bool(beyond),
            )
        )

    # --- current price --------------------------------------------------------
    cur_now: int | None = None
    if g.get("current_index") is not None and g.get("eff_list_cents"):
        cur_now = round(float(g["current_index"]) * float(g["eff_list_cents"]))
    stored_cur = action.get("current_price_cents")
    if cur_now is not None and stored_cur is not None and int(stored_cur) != cur_now:
        move = _rel_move(stored_cur, cur_now)
        beyond = move is not None and move > rc.current_price_change_pct
        diffs.append(
            Difference(
                field="current price",
                stored=f"{stored_cur} cents",
                current=f"{cur_now} cents",
                reason=(
                    f"the pair's realized price moved {move:.1%} since the proposal, "
                    "so the gap it was sized on no longer holds"
                    if beyond
                    else f"the pair's realized price moved {move:.1%}, inside the "
                    f"{rc.current_price_change_pct:.0%} threshold"
                )
                if move is not None
                else "the pair's realized price changed",
                blocking=bool(beyond),
            )
        )

    # --- list staleness -------------------------------------------------------
    provenance = g.get("list_provenance")
    if provenance is not None and str(provenance) != "effective_list":
        diffs.append(
            Difference(
                field="list price provenance",
                stored="effective list",
                current=str(provenance).replace("_", " "),
                reason="the pair's list price is no longer a valid comparison basis",
                blocking=True,
            )
        )

    # --- compliance flags -----------------------------------------------------
    flag_now = (
        "robinson_patman" if ("rp_review_flag" in g and bool(g.get("rp_review_flag"))) else ""
    )
    flag_stored = str(action.get("compliance_flags") or "")
    if "rp_review_flag" not in g:
        flag_stored = ""  # not testable; recorded above, never a silent pass
    if flag_now and not flag_stored:
        diffs.append(
            Difference(
                field="compliance flag",
                stored="none",
                current="price-discrimination review",
                reason="a review flag appeared after the proposal; approval needs a justification",
                blocking=True,
            )
        )
    elif flag_stored and not flag_now:
        diffs.append(
            Difference(
                field="compliance flag",
                stored="price-discrimination review",
                current="none",
                reason="the review flag no longer applies (informational)",
            )
        )

    if not diffs:
        return Revalidation(verdict=VERDICT_UNCHANGED, notes=notes, not_testable=not_testable)
    if any(d.blocking for d in diffs):
        hard = {
            "contract protection",
            "guidance status",
            "pair",
            "scope",
            "proposed price vs floor",
        }
        verdict = VERDICT_INVALID if any(d.field in hard for d in diffs) else VERDICT_RE_REVIEW
        return Revalidation(
            verdict=verdict, differences=diffs, notes=notes, not_testable=not_testable
        )
    return Revalidation(
        verdict=VERDICT_WITHIN_TOLERANCE,
        differences=diffs,
        notes=notes,
        not_testable=not_testable,
    )


def revalidation_rows(
    action_id: str,
    rev: Revalidation,
    *,
    who: str,
    trigger: str,
    at: dt.datetime,
    start_seq: int = 0,
) -> pl.DataFrame:
    """Audit rows for one revalidation: both versions of every difference, so
    the trail keeps what the proposal claimed AND what the world now says."""
    rows: list[dict[str, object]] = []
    if not rev.differences:
        rows.append(
            {
                "action_id": action_id,
                "seq": start_seq,
                "at": at,
                "who": who,
                "trigger": trigger,
                "verdict": rev.verdict,
                "field": None,
                "stored_value": None,
                "current_value": None,
                "reason": rev.message(),
            }
        )
    for i, d in enumerate(rev.differences):
        rows.append(
            {
                "action_id": action_id,
                "seq": start_seq + i,
                "at": at,
                "who": who,
                "trigger": trigger,
                "verdict": rev.verdict,
                "field": d.field,
                "stored_value": None if d.stored is None else str(d.stored),
                "current_value": None if d.current is None else str(d.current),
                "reason": d.reason,
            }
        )
    return pl.DataFrame(rows, schema=REVALIDATION_SCHEMA)
