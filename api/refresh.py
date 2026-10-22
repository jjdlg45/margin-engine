"""Scheduled refreshes (30.3/30.4): FX daily, PPI monthly, replay per tick.
All outbound calls go through the Phase 29 connectors (client-side limiter,
cache, stale fallback); the API NEVER calls an external source inside a
request handler. Every job records a status the UI shows per section, and a
failure marks the section stale with its age instead of breaking the API.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.replay import ReplayFeed
from api.settings import REPO, Settings
from api.store import BundleStore

log = logging.getLogger("margin_api.refresh")

FX_FIXTURE = REPO / "tests" / "fixtures" / "frankfurter_gbp_usd_2009_2011.json"
PPI_FIXTURE = REPO / "tests" / "fixtures" / "bls_ppi_2016_2025.json"


class RefreshScheduler:
    def __init__(self, settings: Settings, store: BundleStore) -> None:
        self.s = settings
        self.store = store
        self.status: dict[str, Any] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self.replay = ReplayFeed(settings, store) if settings.replay_enabled else None

    def _mark(self, section: str, *, stale: bool, detail: str) -> None:
        self.status[section] = {
            "refreshed_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "stale": stale,
            "detail": detail,
        }
        self.store.set_refresh_status(dict(self.status))

    # -- jobs ----------------------------------------------------------------

    def refresh_fx(self) -> None:
        from margin_engine.connectors.base import ConnectorError
        from margin_engine.connectors.fx import fetch_daily_rates

        cache = Path(self.s.db_path).parent / "connector_cache"
        try:
            _df, notes = fetch_daily_rates(
                "GBP",
                "USD",
                "2009-12-01",
                "2011-12-09",
                cache,
                fixture=FX_FIXTURE,
                max_age_days=None,  # closed historical range for the replay data
            )
            stale = any("stale" in n for n in notes)
            self._mark("fx", stale=stale, detail="; ".join(notes) or "ok")
        except ConnectorError as exc:
            self._mark("fx", stale=True, detail=str(exc))

    def refresh_ppi(self) -> None:
        from margin_engine.connectors.base import ConnectorError
        from margin_engine.connectors.bls import fetch_ppi_series

        cache = Path(self.s.db_path).parent / "connector_cache"
        try:
            _df, notes = fetch_ppi_series(
                ["WPU101", "WPU081", "WPU0638"],
                2016,
                2025,
                cache,
                fixture=PPI_FIXTURE,
                max_age_days=None,
            )
            stale = any("stale" in n for n in notes)
            self._mark("ppi", stale=stale, detail="; ".join(notes) or "ok")
        except ConnectorError as exc:
            self._mark("ppi", stale=True, detail=str(exc))

    def replay_tick(self) -> None:
        if self.replay is None:
            return
        try:
            day = self.replay.replay_next_day()
            if day is None:
                self._mark("replay", stale=False, detail="feed exhausted (all days replayed)")
            else:
                self._mark("replay", stale=False, detail=f"replayed through {day}")
        except Exception as exc:
            log.exception("replay tick failed")
            self._mark("replay", stale=True, detail=f"replay failed: {exc}")

    # -- loop ----------------------------------------------------------------

    async def _every(self, seconds: float, fn: Any) -> None:
        while True:
            await asyncio.to_thread(fn)
            await asyncio.sleep(seconds)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._tasks = [
            loop.create_task(self._every(self.s.fx_refresh_s, self.refresh_fx)),
            loop.create_task(self._every(self.s.ppi_refresh_s, self.refresh_ppi)),
        ]
        if self.replay is not None:
            self._tasks.append(
                loop.create_task(self._every(self.s.replay_seconds_per_day, self.replay_tick))
            )

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
