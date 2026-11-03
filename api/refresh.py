"""Scheduled refreshes (30.3/30.4): FX daily, PPI monthly, replay per tick.
All outbound calls go through the Phase 29 connectors (client-side limiter,
cache, stale fallback); the API NEVER calls an external source inside a
request handler. Every job records a status the UI shows per section, and a
failure marks the section stale with its age instead of breaking the API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from functools import partial
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
        # Last failure per system connector (Run 7); cleared on success. The
        # Connections screen reads this next to each connector's last good
        # sync manifest.
        self.connector_errors: dict[str, str] = {}
        self._connector_locks: dict[str, threading.Lock] = {}
        self._connector_tasks: dict[str, asyncio.Task[None]] = {}
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

    # -- system connectors (Run 7, Phases 90 and 94) ---------------------------

    def _schedule_overrides(self) -> dict[str, float]:
        """Per-connector interval overrides set from the Connections screen."""
        p = self.s.connectors_dir / "schedules.json"
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError):
            return {}

    def set_schedule(self, name: str, interval_s: float) -> float:
        """Persist a schedule override and re-arm the connector's task.
        Returns the effective interval (clamped to >= 60 s)."""
        interval = max(float(interval_s), 60.0)
        overrides = self._schedule_overrides()
        overrides[name] = interval
        self.s.connectors_dir.mkdir(parents=True, exist_ok=True)
        p = self.s.connectors_dir / "schedules.json"
        p.write_text(json.dumps(overrides, indent=1), encoding="utf-8")
        task = self._connector_tasks.pop(name, None)
        if task is not None:
            task.cancel()
            loop = task.get_loop()
            self._connector_tasks[name] = loop.create_task(
                self._every(interval, partial(self.connector_tick, name))
            )
        return interval

    def connector_entries(self) -> list[tuple[str, float]]:
        """Parse the connectors spec into (name, interval_s) entries,
        applying any persisted overrides."""
        overrides = self._schedule_overrides()
        out: list[tuple[str, float]] = []
        for part in (p.strip() for p in self.s.connectors_spec.split(",")):
            if not part:
                continue
            name, _, interval_s = part.partition(":")
            name = name.strip()
            try:
                interval = float(interval_s) if interval_s else self.s.connector_default_refresh_s
            except ValueError:
                interval = self.s.connector_default_refresh_s
            out.append((name, max(overrides.get(name, interval), 60.0)))
        return out

    def sync_connector(self, name: str, *, full_resync: bool = False) -> dict[str, Any]:
        """Run one connector sync now (the scheduler job and the manual
        "Sync now" both land here). Raises ConnectorError with a plain
        message; a failure leaves the previous good sync in place. One sync
        per connector at a time: a second caller gets a plain refusal."""
        from margin_engine.connectors.base import ConnectorError
        from margin_engine.connectors.framework import run_sync
        from margin_engine.connectors.registry import build_connector

        lock = self._connector_locks.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            msg = f"a sync for '{name}' is already running; wait for it to finish"
            raise ConnectorError(msg)
        try:
            connector = build_connector(name)
            manifest = run_sync(connector, self.s.connectors_dir, full_resync=full_resync)
            self.connector_errors.pop(name, None)
            return manifest
        finally:
            lock.release()

    def connector_tick(self, name: str) -> None:
        from margin_engine.connectors.base import ConnectorError

        try:
            self.sync_connector(name)
        except ConnectorError as exc:
            self.connector_errors[name] = str(exc)
        except Exception as exc:
            log.exception("connector sync failed: %s", name)
            self.connector_errors[name] = f"sync failed: {exc}"

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
        for name, interval in self.connector_entries():
            self._connector_tasks[name] = loop.create_task(
                self._every(interval, partial(self.connector_tick, name))
            )

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._connector_tasks.values():
            t.cancel()
        self._tasks = []
        self._connector_tasks = {}
