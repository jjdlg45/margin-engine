"""Action center API (Phase 39): maker-checker review over the ActionStore.

Identity: the X-User header names a CONFIGURED user (api.settings users spec) —
local identity, explicitly NOT authentication; real login belongs to a future
hosting phase. Role and policy checks live in margin_engine.actions.review;
lifecycle validity lives in the store. Every endpoint is idempotent: repeating
a transition that already happened returns changed=false instead of an error,
and anything the lifecycle or policy forbids is a 409/403 with the reason.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

import polars as pl
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from api.filters import apply_sort
from margin_engine.actions.model import (
    EVENTS_SCHEMA,
    TransitionError,
    replay_status,
)
from margin_engine.actions.review import (
    EDITABLE_FIELDS,
    ReviewError,
    ReviewPolicy,
    check_edit,
)
from margin_engine.actions.store import ActionStore

router = APIRouter()


def _err(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


_REVIEW_HTTP = {
    "unknown_user": 400,
    "role_denied": 403,
    "self_approval": 403,
    "justification_required": 422,
    "tracker_only": 403,
    "blocked": 409,
    "unknown_status": 400,
    "field_not_editable": 422,
    "status_not_editable": 409,
    "floor_unknown": 422,
    "below_floor": 422,
    "bad_value": 422,
}


def _store(request: Request) -> ActionStore:
    return request.app.state.action_store  # type: ignore[no-any-return]


def _policy(request: Request) -> ReviewPolicy:
    return request.app.state.review_policy  # type: ignore[no-any-return]


def _actions_with_status(store: ActionStore) -> list[dict[str, Any]]:
    actions = store.actions()
    status = replay_status(store.events())
    joined = actions.join(status, on="action_id", how="left", maintain_order="left").with_columns(
        pl.col("status").fill_null("proposed")
    )
    rows = joined.to_dicts()
    for r in rows:
        for k in ("due_date",):
            if r.get(k) is not None:
                r[k] = str(r[k])
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].isoformat()
    return rows


def _match_action(r: dict[str, Any], f: dict[str, Any]) -> bool:
    def lst(key: str) -> list[str]:
        v = f.get(key)
        return [str(x) for x in v] if isinstance(v, list) else []

    impact = r.get("expected_impact_cents")
    lo, hi = f.get("impact_min"), f.get("impact_max")
    if lo is not None and (impact is None or impact / 100 < float(lo)):
        return False
    if hi is not None and (impact is None or impact / 100 > float(hi)):
        return False
    return (
        (not lst("types") or str(r.get("action_type")) in lst("types"))
        and (not lst("statuses") or str(r.get("status")) in lst("statuses"))
        and (not f.get("owner") or r.get("owner") == f["owner"])
        and (not lst("priorities") or str(r.get("priority")) in lst("priorities"))
        and (
            not lst("evidence")
            or str(r.get("evidence_status") or "insufficient_data") in lst("evidence")
        )
        and (not f.get("compliance") or bool(r.get("compliance_flags")))
        and (not f.get("customer") or r.get("customer_id") == f["customer"])
        and (not f.get("item") or r.get("item_id") == f["item"])
    )


def _transition(
    request: Request,
    action_id: str,
    to_status: str,
    *,
    user: str | None,
    note: str,
) -> JSONResponse:
    if not user:
        return _err(
            400,
            "missing_user",
            "send X-User: a configured user name (local identity, not authentication)",
        )
    store = _store(request)
    policy = _policy(request)
    action = store.get_action(action_id)
    if action is None:
        return _err(404, "not_found", f"unknown action {action_id!r}")
    events = store.events()
    current = (
        replay_status(events).filter(pl.col("action_id") == action_id)["status"].to_list()
        or ["proposed"]
    )[0]
    if current == to_status:
        return JSONResponse({"action_id": action_id, "status": current, "changed": False})
    try:
        policy.check_transition(action, events, user=user, to_status=to_status, note=note)
        store.transition(action_id, who=user, to_status=to_status, note=note)
    except ReviewError as exc:
        return _err(_REVIEW_HTTP.get(exc.code, 400), exc.code, str(exc))
    except TransitionError as exc:
        return _err(409, "invalid_transition", str(exc))
    return JSONResponse({"action_id": action_id, "status": to_status, "changed": True})


@router.get("/api/pages/actions")
async def actions_page(request: Request) -> Any:
    """The Action center page payload in API mode, computed LIVE from the
    action store (same shape as the static bundle's actions.json; read_only
    false, history real)."""
    from margin_engine.actions.tracking import _calibration
    from margin_engine.config import load_rules

    rows = _actions_with_status(_store(request))
    outcomes = _store(request).outcomes()
    rules = load_rules(request.app.state.settings.rules_path)
    by_status: dict[str, int] = {}
    impact_by_evidence: dict[str, int] = {}
    impact_by_basis: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        ev = str(r.get("evidence_status") or "insufficient_data")
        impact_by_evidence[ev] = impact_by_evidence.get(ev, 0) + int(
            r.get("expected_impact_cents") or 0
        )
        basis = str(r.get("impact_basis"))
        impact_by_basis[basis] = impact_by_basis.get(basis, 0) + int(
            r.get("expected_impact_cents") or 0
        )
    measured = outcomes.filter(pl.col("outcome_status") == "measured")
    realizations = sorted(measured["realization_pct"].drop_nulls().to_list())
    calibration = [
        {
            "bucket": str(c["bucket"]),
            "n_measured": int(c["n_measured"]),
            "median_abs_volume_error_log_points": c["median_abs_volume_error_log_points"],
            "median_realization_pct": c["median_realization_pct"],
            "status": str(c["status"]),
        }
        for c in _calibration(outcomes, rules).iter_rows(named=True)
    ]
    return {
        "read_only": False,
        "demo_history": False,
        "summary": {
            "n_actions": len(rows),
            "by_status": dict(sorted(by_status.items())),
            "impact_by_evidence_cents": dict(sorted(impact_by_evidence.items())),
            "impact_by_basis_cents": dict(sorted(impact_by_basis.items())),
            "n_blocked": sum(1 for r in rows if r["blocked"]),
            "n_compliance_flagged": sum(1 for r in rows if r.get("compliance_flags")),
            "n_measured": measured.height,
            "median_realization_pct": (
                realizations[len(realizations) // 2] if realizations else None
            ),
        },
        "calibration": calibration,
        "facets": {
            "types": sorted({str(r["action_type"]) for r in rows}),
            "statuses": sorted({str(r["status"]) for r in rows}),
            "owners": sorted({str(r["owner"]) for r in rows}),
            "priorities": sorted({str(r["priority"]) for r in rows}),
            "evidence": sorted(
                {str(r.get("evidence_status") or "insufficient_data") for r in rows}
            ),
        },
        "notes": [],
    }


@router.get("/api/actions/users")
async def actions_users(request: Request) -> Any:
    """Configured users and their roles — LOCAL IDENTITY for the demo review
    flow, explicitly not authentication."""
    policy = _policy(request)
    return {
        "users": [
            {"name": name, "roles": sorted(roles)} for name, roles in sorted(policy.users.items())
        ],
        "note": "Local identity, not authentication; real login belongs to a future hosting phase.",
    }


@router.get("/api/actions/outcomes")
async def actions_outcomes(request: Request) -> Any:
    """Measured and insufficient outcomes from the latest tracker run (the
    `margin-engine track` command persists them)."""
    rows = _store(request).outcomes().to_dicts()
    for r in rows:
        r["implemented_on"] = str(r["implemented_on"])
    return {"items": rows}


@router.get("/api/actions/summary")
async def actions_summary(request: Request) -> Any:
    rows = _actions_with_status(_store(request))
    by_status: dict[str, int] = {}
    impact_by_evidence: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        ev = str(r.get("evidence_status") or "insufficient_data")
        impact_by_evidence[ev] = impact_by_evidence.get(ev, 0) + int(
            r.get("expected_impact_cents") or 0
        )
    return {
        "n_actions": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "impact_by_evidence_cents": dict(sorted(impact_by_evidence.items())),
        "n_blocked": sum(1 for r in rows if r["blocked"]),
        "n_compliance_flagged": sum(1 for r in rows if r.get("compliance_flags")),
    }


@router.get("/api/actions/{action_id}")
async def action_detail(action_id: str, request: Request) -> Any:
    store = _store(request)
    action = store.get_action(action_id)
    if action is None:
        return _err(404, "not_found", f"unknown action {action_id!r}")
    events = store.events().filter(pl.col("action_id") == action_id)
    status = (events.sort("seq")["to_status"].to_list() or ["proposed"])[-1]
    row = dict(action)
    if row.get("due_date") is not None:
        row["due_date"] = str(row["due_date"])
    created = row.get("created_at")
    if isinstance(created, dt.datetime):
        row["created_at"] = created.isoformat()
    ev_rows = events.sort("seq").to_dicts()
    for e in ev_rows:
        e["at"] = e["at"].isoformat()
    edit_rows = store.edits(action_id).to_dicts()
    for e in edit_rows:
        e["at"] = e["at"].isoformat()
    outcome = None
    orows = store.outcomes().filter(pl.col("action_id") == action_id)
    if orows.height:
        outcome = orows.to_dicts()[0]
        outcome["implemented_on"] = str(outcome["implemented_on"])
        outcome.pop("action_id", None)
        outcome.pop("customer_id", None)
        outcome.pop("item_id", None)
        outcome.pop("volume_treatment", None)
        outcome.pop("evidence_status", None)
    return {
        "action": row,
        "status": status,
        "events": ev_rows,
        "edits": edit_rows,
        "outcome": outcome,
    }


@router.get("/api/actions")
async def actions_table(
    request: Request,
    page: int = 1,
    page_size: int = 50,
    sort: str | None = None,
    filters: str | None = None,
) -> Any:
    import json as _json

    if page < 1 or page_size < 1 or page_size > 5000:
        return _err(400, "bad_pagination", "page must be >= 1 and page_size in [1, 5000]")
    parsed: dict[str, Any] = {}
    if filters:
        try:
            parsed = _json.loads(filters)
            if not isinstance(parsed, dict):
                raise ValueError("filters must be a JSON object")
        except ValueError as exc:
            return _err(400, "bad_filters", f"malformed filters parameter: {exc}")
    if sort is not None and not sort.lstrip("-").replace("_", "").isalnum():
        return _err(400, "bad_sort", "malformed sort parameter")
    all_rows = _actions_with_status(_store(request))
    rows = [r for r in all_rows if _match_action(r, parsed)]
    ordered = apply_sort(rows, sort)
    start = (page - 1) * page_size
    chunk = ordered[start : start + page_size]
    return {
        "items": chunk,
        "total": len(ordered),
        "total_unfiltered": len(all_rows),
        "page": page,
        "page_size": page_size,
        "has_more": start + page_size < len(ordered),
    }


@router.post("/api/actions/{action_id}/submit")
async def submit(
    action_id: str,
    request: Request,
    body: dict[str, Any] | None = None,
    x_user: str | None = Header(default=None),
) -> Any:
    return _transition(
        request, action_id, "under_review", user=x_user, note=str((body or {}).get("note", ""))
    )


@router.post("/api/actions/{action_id}/approve")
async def approve(
    action_id: str,
    request: Request,
    body: dict[str, Any] | None = None,
    x_user: str | None = Header(default=None),
) -> Any:
    return _transition(
        request, action_id, "approved", user=x_user, note=str((body or {}).get("reason", ""))
    )


@router.post("/api/actions/{action_id}/reject")
async def reject(
    action_id: str,
    request: Request,
    body: dict[str, Any] | None = None,
    x_user: str | None = Header(default=None),
) -> Any:
    return _transition(
        request, action_id, "rejected", user=x_user, note=str((body or {}).get("reason", ""))
    )


@router.post("/api/actions/{action_id}/rework")
async def rework(
    action_id: str,
    request: Request,
    body: dict[str, Any] | None = None,
    x_user: str | None = Header(default=None),
) -> Any:
    return _transition(
        request, action_id, "proposed", user=x_user, note=str((body or {}).get("note", ""))
    )


@router.post("/api/actions/{action_id}/implement")
async def implement(
    action_id: str,
    request: Request,
    body: dict[str, Any] | None = None,
    x_user: str | None = Header(default=None),
) -> Any:
    return _transition(
        request, action_id, "implemented", user=x_user, note=str((body or {}).get("note", ""))
    )


@router.post("/api/actions/bulk-approve")
async def bulk_approve(
    request: Request, body: dict[str, Any], x_user: str | None = Header(default=None)
) -> Any:
    """Per-item guardrail checks: items that fail are refused with the reason
    (their status is untouched — refusing to approve is not a human rejection);
    items already approved report unchanged."""
    if not x_user:
        return _err(400, "missing_user", "send X-User (local identity, not authentication)")
    ids = body.get("action_ids")
    note = str(body.get("reason", ""))
    if not isinstance(ids, list) or not ids:
        return _err(400, "bad_request", "action_ids must be a non-empty list")
    store = _store(request)
    policy = _policy(request)
    results: list[dict[str, Any]] = []
    for action_id in [str(i) for i in ids]:
        action = store.get_action(action_id)
        if action is None:
            results.append(
                {"action_id": action_id, "outcome": "refused", "reason": "unknown action"}
            )
            continue
        events = store.events()
        current = (
            replay_status(events).filter(pl.col("action_id") == action_id)["status"].to_list()
            or ["proposed"]
        )[0]
        if current == "approved":
            results.append(
                {"action_id": action_id, "outcome": "unchanged", "reason": "already approved"}
            )
            continue
        try:
            policy.check_transition(action, events, user=x_user, to_status="approved", note=note)
            store.transition(action_id, who=x_user, to_status="approved", note=note)
            results.append({"action_id": action_id, "outcome": "approved", "reason": ""})
        except (ReviewError, TransitionError) as exc:
            code = getattr(exc, "code", "invalid_transition")
            results.append(
                {"action_id": action_id, "outcome": "refused", "reason": f"{code}: {exc}"}
            )
    return {"results": results}


@router.post("/api/actions/price-export")
async def price_export(
    request: Request, body: dict[str, Any] | None = None, x_user: str | None = Header(default=None)
) -> Any:
    """Build the ERP price file from approved price actions (Phase 40). The
    response carries the diff summary and the CSV text (the UI offers it as a
    download); dry_run previews without changing state. Idempotent: the file
    always holds every approved-or-exported price action, and only
    still-approved ones transition."""
    if not x_user:
        return _err(400, "missing_user", "send X-User (local identity, not authentication)")
    store = _store(request)
    policy = _policy(request)
    try:
        policy.require_role(x_user, "analyst")
    except ReviewError as exc:
        return _err(_REVIEW_HTTP.get(exc.code, 400), exc.code, str(exc))
    from margin_engine.actions.export import export_price_file
    from margin_engine.config import load_rules

    rules = load_rules(request.app.state.settings.rules_path)
    dry_run = bool((body or {}).get("dry_run", False))
    result = export_price_file(store, rules, None, who=x_user, dry_run=dry_run)
    return {
        "summary": result.summary,
        "csv": result.csv_text,
        "n_transitioned": result.n_transitioned,
        "dry_run": dry_run,
    }


@router.patch("/api/actions/{action_id}")
async def edit_action(
    action_id: str,
    request: Request,
    body: dict[str, Any],
    x_user: str | None = Header(default=None),
) -> Any:
    if not x_user:
        return _err(400, "missing_user", "send X-User (local identity, not authentication)")
    store = _store(request)
    policy = _policy(request)
    action = store.get_action(action_id)
    if action is None:
        return _err(404, "not_found", f"unknown action {action_id!r}")
    status = (
        replay_status(store.events()).filter(pl.col("action_id") == action_id)["status"].to_list()
        or ["proposed"]
    )[0]
    try:
        policy.require_role(x_user, "analyst")
        changed: list[str] = []
        for field, value in body.items():
            if field == "due_date" and isinstance(value, str):
                value = dt.date.fromisoformat(value)
            check_edit(action, status, field=field, value=value)
            store.apply_edit(action_id, who=x_user, field=field, value=value)
            changed.append(field)
    except ReviewError as exc:
        return _err(_REVIEW_HTTP.get(exc.code, 400), exc.code, str(exc))
    except ValueError as exc:
        return _err(422, "bad_value", str(exc))
    return {"action_id": action_id, "changed_fields": changed}


@router.post("/api/actions")
async def propose(
    request: Request, body: dict[str, Any], x_user: str | None = Header(default=None)
) -> Any:
    """Manually propose an action. Price-type proposals are guardrail-checked
    against the floor on record for the pair; a pair with no floor on record
    is refused rather than guessed."""
    if not x_user:
        return _err(400, "missing_user", "send X-User (local identity, not authentication)")
    store = _store(request)
    policy = _policy(request)
    try:
        policy.require_role(x_user, "analyst")
    except ReviewError as exc:
        return _err(_REVIEW_HTTP.get(exc.code, 400), exc.code, str(exc))
    action_type = str(body.get("action_type", ""))
    title = str(body.get("title", "")).strip()
    rationale = str(body.get("rationale", "")).strip()
    customer_id = body.get("customer_id")
    item_id = body.get("item_id")
    proposed_price = body.get("proposed_price_cents")
    if action_type not in (
        "price_change",
        "below_floor_fix",
        "cost_pass_through",
        "service_policy",
        "rebate_discount_review",
    ):
        return _err(422, "bad_type", f"unknown action_type {action_type!r}")
    if not title or not rationale:
        return _err(422, "bad_request", "title and rationale are required")
    floor: int | None = None
    if action_type in ("price_change", "below_floor_fix"):
        if not customer_id or not item_id:
            return _err(422, "bad_request", "price actions need customer_id and item_id")
        existing = store.actions().filter(
            (pl.col("customer_id") == str(customer_id)) & (pl.col("item_id") == str(item_id))
        )
        floors = [f for f in existing["floor_price_cents"].to_list() if f is not None]
        floor = max(floors) if floors else None
        if proposed_price is not None:
            if floor is None:
                return _err(
                    422,
                    "floor_unknown",
                    "no binding floor on record for this pair; the engine must "
                    "compute floors before a manual price can be checked",
                )
            if int(proposed_price) < floor:
                return _err(
                    422,
                    "below_floor",
                    f"proposed price {proposed_price} cents is below the binding "
                    f"floor {floor} cents",
                )
    now = dt.datetime.now(dt.UTC)
    raw = f"manual|{action_type}|{customer_id}|{item_id}|{x_user}|{now.isoformat()}|{title}"
    action_id = f"act_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
    action = pl.DataFrame(
        {
            "action_id": [action_id],
            "action_type": [action_type],
            "title": [title],
            "rationale": [rationale],
            "customer_id": [str(customer_id) if customer_id else None],
            "item_id": [str(item_id) if item_id else None],
            "scope": ["manual"],
            "uom": [None],
            "segment_leaf": [None],
            "guidance_status": [None],
            "evidence_status": ["manual"],
            "volume_treatment": [None],
            "predicted_volume_ratio": [None],
            "impact_basis": ["manual_proposal"],
            "expected_impact_cents": [
                int(body["expected_impact_cents"])
                if body.get("expected_impact_cents") is not None
                else None
            ],
            "current_price_cents": [None],
            "proposed_price_cents": [int(proposed_price) if proposed_price is not None else None],
            "floor_price_cents": [floor],
            "priority": [str(body.get("priority", "medium"))],
            "owner": [str(body.get("owner", x_user))],
            "due_date": [None],
            "compliance_flags": [""],
            "blocked": [False],
            "blocked_reason": [None],
            "created_at": [now],
        },
    )
    from margin_engine.actions.generate import ACTIONS_SCHEMA

    action = action.cast(ACTIONS_SCHEMA)  # type: ignore[arg-type]
    birth = pl.DataFrame(
        {
            "action_id": [action_id],
            "seq": [0],
            "who": [x_user],
            "at": [now],
            "from_status": [None],
            "to_status": ["proposed"],
            "note": ["manually proposed"],
        },
        schema=EVENTS_SCHEMA,
    )
    store.add_action(action, birth)
    return {"action_id": action_id, "status": "proposed"}


__all__ = ["EDITABLE_FIELDS", "router"]
