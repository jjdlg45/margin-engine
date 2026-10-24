"""FastAPI service (30.1) implementing docs/api/openapi.yaml exactly, mounted
under /api (the UI's default VITE_API_BASE_URL). Responses carry ETag and
Last-Modified from the loaded bundle version (30.2); inbound rate limiting
returns 429 with Retry-After (30.3)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from api.actions_routes import router as actions_router
from api.filters import paginate
from api.ratelimit import TokenBucketLimiter
from api.refresh import RefreshScheduler
from api.settings import Settings
from api.store import PAGES, TABLES, BundleStore
from margin_engine.actions.review import ReviewPolicy
from margin_engine.actions.store import ActionStore

log = logging.getLogger("margin_api")

API_VERSION = "1.0.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()
    store = BundleStore(s.db_path)
    scheduler = RefreshScheduler(s, store)
    action_store = ActionStore(s.actions_db)
    review_policy = ReviewPolicy.from_spec(s.users_spec)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Bundle preference: a fresh export on disk, else the last persisted
        # version in DuckDB, else fail loudly at first request.
        if (s.bundle_dir / "app.json").exists():
            version = store.load_bundle(s.bundle_dir)
            log.info("bundle loaded from %s (version %s)", s.bundle_dir, version)
        elif store.load_from_db():
            log.info("bundle restored from DuckDB (version %s)", store.snapshot.version)
        else:
            log.warning("no bundle found in %s or DuckDB", s.bundle_dir)
        # Action store seed (Phase 39): merge engine-generated actions without
        # ever touching existing actions or their review history.
        if s.actions_seed_dir is not None:
            import polars as pl

            a_pq = s.actions_seed_dir / "actions.parquet"
            e_pq = s.actions_seed_dir / "action_events.parquet"
            if a_pq.exists() and e_pq.exists():
                added = action_store.merge_new(pl.read_parquet(a_pq), pl.read_parquet(e_pq))
                log.info("action store: %d new action(s) merged from %s", added, s.actions_seed_dir)
            else:
                log.warning("actions seed dir %s has no actions exports", s.actions_seed_dir)
        scheduler.start(asyncio.get_running_loop())
        yield
        scheduler.stop()

    app = FastAPI(title="Margin Engine BFF", version=API_VERSION, lifespan=lifespan)
    limiter = TokenBucketLimiter(s.rate_limit_per_minute, s.rate_limit_burst)
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.settings = s
    app.state.action_store = action_store
    app.state.review_policy = review_policy
    app.include_router(actions_router)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next: Any) -> Response:
        client = request.client.host if request.client else "unknown"
        retry = limiter.check(client)
        if retry is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded; retry later"},
                headers={"Retry-After": str(max(1, round(retry)))},
            )
        return await call_next(request)  # type: ignore[no-any-return]

    def _cached_json(request: Request, payload: Any) -> Response:
        snap = store.snapshot
        if request.headers.get("if-none-match") == snap.etag:
            return Response(status_code=304)
        return JSONResponse(
            content=payload,
            headers={
                "ETag": snap.etag,
                "Last-Modified": snap.last_modified,
                "Cache-Control": "public, max-age=0, must-revalidate",
            },
        )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        try:
            version = store.snapshot.version
        except RuntimeError:
            return {"status": "no_bundle"}
        return {"status": "ok", "bundle_version": version}

    @app.get("/api/drift")
    async def drift(request: Request) -> Any:
        """Latest drift comparison (Phase 43): flags plus the raw numbers
        behind every comparison, flagged or not."""
        from margin_engine.drift import DriftStore

        return DriftStore(s.drift_dir).latest()

    @app.get("/api/version")
    async def version() -> dict[str, Any]:
        snap = store.snapshot
        return {
            "api_version": API_VERSION,
            "bundle_version": snap.app.get("bundle_version"),
            "engine_version": snap.app.get("engine_version"),
            "loaded_utc": snap.loaded_utc.isoformat(timespec="seconds"),
        }

    @app.get("/api/app")
    async def get_app(request: Request) -> Response:
        snap = store.snapshot
        payload = dict(snap.app)
        # Live-mode additions (30.4): data-as-of and per-section refresh
        # status. Additive fields; the contract's required AppData fields are
        # untouched.
        if snap.refresh_status:
            payload["refresh_status"] = snap.refresh_status
            replay = snap.refresh_status.get("replay") or {}
            if replay.get("data_as_of"):
                payload["data_as_of"] = replay["data_as_of"]
        # Drift flags (Phase 43): plain-English changes vs the previous
        # refresh; an empty list means a comparison ran and found nothing.
        from margin_engine.drift import DriftStore

        payload["drift_flags"] = DriftStore(s.drift_dir).latest()["flags"]
        return _cached_json(request, payload)

    @app.get("/api/pages/{page}")
    async def get_page(page: str, request: Request) -> Response:
        if page not in PAGES:
            return JSONResponse(status_code=404, content={"detail": f"unknown page '{page}'"})
        return _cached_json(request, store.snapshot.pages[page])

    @app.get("/api/tables/guidance/explanations/{customer_id}/{item_id}")
    async def get_explanation(customer_id: str, item_id: str, request: Request) -> Response:
        text = store.snapshot.explanations.get(f"{customer_id}|{item_id}")
        if text is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"no explanation for {customer_id}/{item_id}"},
            )
        return _cached_json(request, {"explanation": text})

    @app.get("/api/tables/{table}")
    async def get_table(
        table: str,
        request: Request,
        page: int = 1,
        page_size: int = 50,
        sort: str | None = None,
        filters: str | None = None,
    ) -> Response:
        if table not in TABLES:
            return JSONResponse(status_code=404, content={"detail": f"unknown table '{table}'"})
        if page < 1 or page_size < 1 or page_size > 5000:
            return JSONResponse(
                status_code=400,
                content={"detail": "page must be >= 1 and page_size in [1, 5000]"},
            )
        parsed: dict[str, Any] = {}
        if filters:
            try:
                parsed = json.loads(filters)
                if not isinstance(parsed, dict):
                    raise ValueError("filters must be a JSON object")
            except ValueError as exc:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"malformed filters parameter: {exc}"},
                )
        if sort is not None and not sort.lstrip("-").replace("_", "").isalnum():
            return JSONResponse(status_code=400, content={"detail": "malformed sort parameter"})
        result = paginate(store.snapshot.tables[table], parsed, sort, page, page_size, table)
        return _cached_json(request, result)

    return app


app = create_app()
