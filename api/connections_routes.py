"""Connections endpoints (Run 7, Phase 94).

The Connections screen in local and API mode: each connector's status, what
it provides, last sync time and record counts, warnings in plain English,
"Check connection", "Sync now", and schedule settings. In production mode
every endpoint here requires the admin role; local single-user mode does
not gate (and the UI says identity is not authentication there).

The static GitHub Pages demo never calls these endpoints - it shows an
explanatory page from the bundle instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.auth import ROLE_ADMIN, AuthError

router = APIRouter()


def _require_connections_access(request: Request) -> None:
    """Admin-only in production mode; open in local single-user mode."""
    auth = request.app.state.auth
    if not auth.production:
        return
    principal = auth.resolve(
        header_user=request.headers.get("x-user"),
        authorization=request.headers.get("authorization"),
    )
    if principal is None:
        raise AuthError(401, "authentication_required", "authenticate at /api/auth/login")
    if not principal.has(ROLE_ADMIN):
        raise AuthError(403, "admin_required", "configuring connectors requires the admin role")


def _sync_summary(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    return {
        "sync_id": manifest.get("sync_id"),
        "synced_utc": manifest.get("synced_utc"),
        "window": manifest.get("window"),
        "records": manifest.get("records", {}),
        "warnings": manifest.get("warnings", []),
        "unchanged_rerun": bool(manifest.get("unchanged_rerun", False)),
    }


def _connector_state(request: Request, name: str) -> dict[str, Any]:
    from margin_engine.connectors.credentials import CredentialStore
    from margin_engine.connectors.framework import SyncStore
    from margin_engine.connectors.registry import build_connector

    s = request.app.state.settings
    scheduler = request.app.state.scheduler
    creds = CredentialStore()
    info = build_connector(name, creds).describe()
    missing = creds.missing(info)
    schedule = dict(scheduler.connector_entries())
    latest = SyncStore(s.connectors_dir, name).latest()
    return {
        "name": info.name,
        "system": info.system,
        "description": info.description,
        "produces": list(info.produces),
        "scopes": list(info.scopes),
        "live_verification": info.live_verification,
        "configured": not missing,
        "missing_settings": [{"env": spec.env, "purpose": spec.purpose} for spec in missing],
        "scheduled": name in schedule,
        "schedule_interval_s": schedule.get(name),
        "last_sync": _sync_summary(latest),
        "last_error": scheduler.connector_errors.get(name),
    }


@router.get("/api/connections")
async def list_connections(request: Request) -> Any:
    from margin_engine.connectors.registry import connector_names

    _require_connections_access(request)
    return {
        "mode": request.app.state.settings.mode,
        "connectors": [_connector_state(request, n) for n in connector_names()],
    }


def _known_or_404(name: str) -> JSONResponse | None:
    from margin_engine.connectors.registry import connector_names

    if name not in connector_names():
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown connector '{name}'", "code": "unknown_connector"},
        )
    return None


@router.post("/api/connections/{name}/check")
async def check_connection(request: Request, name: str) -> Any:
    import asyncio

    from margin_engine.connectors.credentials import CredentialStore
    from margin_engine.connectors.registry import build_connector

    _require_connections_access(request)
    missing = _known_or_404(name)
    if missing is not None:
        return missing
    connector = build_connector(name, CredentialStore())
    result = await asyncio.to_thread(connector.check)
    request.app.state.audit.record(
        action="connections.check", user_id=_caller(request), status=200, detail=name
    )
    return {"ok": result.ok, "message": result.message}


@router.post("/api/connections/{name}/sync")
async def sync_connection(request: Request, name: str) -> Any:
    import asyncio

    from margin_engine.connectors.base import ConnectorError

    _require_connections_access(request)
    missing = _known_or_404(name)
    if missing is not None:
        return missing
    body: dict[str, Any] = {}
    if (request.headers.get("content-type") or "").startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            body = {}
    full_resync = bool(body.get("full_resync", False))
    scheduler = request.app.state.scheduler
    try:
        manifest = await asyncio.to_thread(
            lambda: scheduler.sync_connector(name, full_resync=full_resync)
        )
    except ConnectorError as exc:
        request.app.state.audit.record(
            action="connections.sync", user_id=_caller(request), status=409, detail=name
        )
        return JSONResponse(status_code=409, content={"detail": str(exc), "code": "sync_failed"})
    request.app.state.audit.record(
        action="connections.sync", user_id=_caller(request), status=200, detail=name
    )
    return {"status": "ok", "sync": _sync_summary(manifest)}


@router.put("/api/connections/{name}/schedule")
async def set_schedule(request: Request, name: str) -> Any:
    _require_connections_access(request)
    missing = _known_or_404(name)
    if missing is not None:
        return missing
    try:
        body = await request.json()
        interval = float(body["interval_s"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse(
            status_code=400,
            content={
                "detail": 'body must be {"interval_s": <seconds>}; 60 is the minimum',
                "code": "bad_schedule",
            },
        )
    scheduler = request.app.state.scheduler
    effective = scheduler.set_schedule(name, interval)
    scheduled = name in dict(scheduler.connector_entries())
    note = (
        ""
        if scheduled
        else (
            " The connector is not in MARGIN_API_CONNECTORS, so the interval is "
            "stored but no job runs until it is enabled there."
        )
    )
    request.app.state.audit.record(
        action="connections.schedule", user_id=_caller(request), status=200, detail=name
    )
    return {
        "status": "ok",
        "interval_s": effective,
        "scheduled": scheduled,
        "note": note.strip(),
    }


def _caller(request: Request) -> str | None:
    auth = request.app.state.auth
    principal = auth.resolve(
        header_user=request.headers.get("x-user"),
        authorization=request.headers.get("authorization"),
    )
    return principal.user_id if principal is not None else None
