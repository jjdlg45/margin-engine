"""Maker-checker review policy (Phase 39).

Roles: analyst (proposes, edits, submits, exports, marks implemented),
approver (approves or rejects), viewer (reads only). A user may hold several
roles. Identity in local mode is a CONFIGURED USER NAME the client sends with
each request — explicitly not authentication (real login belongs to a future
hosting phase); the policy layer is the same either way.

Rules enforced here (the store already enforces the lifecycle itself):
- role gates per transition; viewers change nothing;
- separation of duties: an approver cannot approve an action they proposed or
  submitted for review;
- a written justification is required to approve an action carrying a
  compliance flag;
- blocked actions (contract-protected) never advance;
- 'measured' is the tracker's transition (Phase 41), never a user's.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

ROLES = ("analyst", "approver", "viewer")

# Which role may PERFORM a transition to this status.
TRANSITION_ROLES: dict[str, tuple[str, ...]] = {
    "under_review": ("analyst",),
    "approved": ("approver",),
    "rejected": ("approver",),
    "proposed": ("analyst",),  # rework after rejection
    "exported": ("analyst",),  # normally via the price-file export (Phase 40)
    "implemented": ("analyst",),
    "measured": (),  # results tracker only (Phase 41)
}


class ReviewError(ValueError):
    """A review-policy violation; .code is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReviewPolicy:
    """users: configured name -> set of roles. Local identity, not auth."""

    users: dict[str, frozenset[str]]

    @classmethod
    def from_spec(cls, spec: str) -> ReviewPolicy:
        """Parse 'alice:analyst,bob:approver,pat:analyst+approver'."""
        users: dict[str, frozenset[str]] = {}
        for part in filter(None, (p.strip() for p in spec.split(","))):
            name, _, roles_s = part.partition(":")
            roles = frozenset(r.strip() for r in roles_s.split("+") if r.strip())
            bad = roles - set(ROLES)
            if not name or not roles or bad:
                msg = (
                    f"malformed user spec {part!r}: expected name:role[+role] "
                    f"with roles from {ROLES}"
                )
                raise ValueError(msg)
            users[name.strip()] = roles
        if not users:
            msg = "no users configured"
            raise ValueError(msg)
        return cls(users=users)

    def roles(self, user: str) -> frozenset[str]:
        if user not in self.users:
            msg = (
                f"unknown user {user!r}: identity is a configured user name "
                f"(local mode, not authentication); configured users: "
                f"{sorted(self.users)}"
            )
            raise ReviewError("unknown_user", msg)
        return self.users[user]

    def require_role(self, user: str, role: str) -> None:
        if role not in self.roles(user):
            msg = f"user {user!r} lacks the {role!r} role"
            raise ReviewError("role_denied", msg)

    def check_transition(
        self,
        action: dict[str, object],
        events: pl.DataFrame,
        *,
        user: str,
        to_status: str,
        note: str,
    ) -> None:
        """Raise ReviewError when policy forbids this transition. The caller
        still goes through the store/append_event for lifecycle validity."""
        allowed_roles = TRANSITION_ROLES.get(to_status)
        if allowed_roles is None:
            msg = f"unknown target status {to_status!r}"
            raise ReviewError("unknown_status", msg)
        if not allowed_roles:
            msg = "'measured' is set by the results tracker, never by a user"
            raise ReviewError("tracker_only", msg)
        user_roles = self.roles(user)
        if not user_roles.intersection(allowed_roles):
            msg = (
                f"user {user!r} (roles {sorted(user_roles)}) may not move an "
                f"action to {to_status!r} (needs {list(allowed_roles)})"
            )
            raise ReviewError("role_denied", msg)
        if bool(action.get("blocked")):
            msg = f"action is blocked: {action.get('blocked_reason')}"
            raise ReviewError("blocked", msg)
        if to_status in ("approved", "rejected"):
            mine = events.filter(
                (pl.col("action_id") == action["action_id"])
                & pl.col("to_status").is_in(["proposed", "under_review"])
                & (pl.col("who") != "engine")
            )
            owners = set(mine["who"].to_list())
            if to_status == "approved" and user in owners:
                msg = (
                    f"separation of duties: {user!r} proposed or submitted this "
                    "action and cannot approve it"
                )
                raise ReviewError("self_approval", msg)
        if (
            to_status == "approved"
            and str(action.get("compliance_flags") or "")
            and not note.strip()
        ):
            msg = (
                "this action carries a compliance flag "
                f"({action.get('compliance_flags')}): approval requires a "
                "written justification in the note"
            )
            raise ReviewError("justification_required", msg)


# Fields an analyst may edit, while the action is not yet approved.
EDITABLE_FIELDS = ("owner", "due_date", "title", "rationale", "proposed_price_cents")
EDITABLE_STATUSES = ("proposed", "under_review", "rejected")


def check_edit(
    action: dict[str, object],
    status: str,
    *,
    field: str,
    value: object,
) -> None:
    """Edit guardrails: whitelist, pre-approval statuses only, blocked actions
    untouched, and a price edit never goes below the binding floor."""
    if field not in EDITABLE_FIELDS:
        msg = f"field {field!r} is not editable (editable: {list(EDITABLE_FIELDS)})"
        raise ReviewError("field_not_editable", msg)
    if status not in EDITABLE_STATUSES:
        msg = f"an action in status {status!r} can no longer be edited"
        raise ReviewError("status_not_editable", msg)
    if bool(action.get("blocked")):
        msg = f"action is blocked: {action.get('blocked_reason')}"
        raise ReviewError("blocked", msg)
    if field == "proposed_price_cents":
        floor = action.get("floor_price_cents")
        if not isinstance(value, int):
            msg = "proposed_price_cents must be integer cents"
            raise ReviewError("bad_value", msg)
        if floor is None:
            msg = (
                "no binding floor on record for this pair: a price edit cannot "
                "be guardrail-checked, so it is refused (run the engine to "
                "compute floors first)"
            )
            raise ReviewError("floor_unknown", msg)
        if value < int(floor):  # type: ignore[call-overload]
            msg = (
                f"proposed price {value} cents is below the binding floor "
                f"{floor} cents; the floor exists to guarantee the margin"
            )
            raise ReviewError("below_floor", msg)
