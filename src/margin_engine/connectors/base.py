"""Connector plumbing (Run 4, Phase 29): client-side rate limiting with
exponential backoff, a JSON file cache with timestamps, and stale-fallback.

Rules every connector follows (docs/data-sources.md):
- never call the same resource more than once per refresh window (the cache
  is checked first, keyed by the full request);
- respect documented limits with a client-side limiter and backoff;
- on failure, fail with an actionable message and fall back to the last
  cached value, labeled stale with its age;
- live calls happen only in refresh jobs and `-m network` tests.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ConnectorError(RuntimeError):
    """Actionable connector failure (never a raw stack trace to the user)."""


@dataclass
class RateLimiter:
    """Minimum spacing between outbound calls to one host."""

    min_interval_s: float = 1.0
    _last: float = field(default=0.0, repr=False)

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval_s:
            time.sleep(self.min_interval_s - delta)
        self._last = time.monotonic()


def get_json(
    url: str,
    limiter: RateLimiter,
    *,
    retries: int = 3,
    backoff_s: float = 2.0,
    timeout_s: float = 30.0,
    source_name: str = "external source",
) -> Any:
    """GET a JSON document with rate limiting and exponential backoff."""
    last_err: Exception | None = None
    for attempt in range(retries):
        limiter.wait()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "margin-engine/0.1"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(backoff_s * (2**attempt))
    msg = (
        f"{source_name} unavailable after {retries} attempts ({last_err}). "
        f"URL: {url}. The engine falls back to the last cached value when one "
        f"exists (labeled stale); otherwise rerun when the source is reachable."
    )
    raise ConnectorError(msg)


@dataclass
class CachedResponse:
    data: Any
    fetched_utc: datetime
    stale: bool = False

    @property
    def age_days(self) -> float:
        return (datetime.now(UTC) - self.fetched_utc).total_seconds() / 86_400


class JsonCache:
    """One JSON file per resource key, with the fetch timestamp recorded."""

    def __init__(self, cache_dir: Path) -> None:
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def load(self, key: str) -> CachedResponse | None:
        p = self._path(key)
        if not p.exists():
            return None
        payload = json.loads(p.read_text(encoding="utf-8"))
        return CachedResponse(
            data=payload["data"],
            fetched_utc=datetime.fromisoformat(payload["fetched_utc"]),
        )

    def store(self, key: str, data: Any) -> None:
        payload = {"fetched_utc": datetime.now(UTC).isoformat(timespec="seconds"), "data": data}
        self._path(key).write_text(json.dumps(payload, indent=1), encoding="utf-8", newline="\n")


def fetch_cached(
    key: str,
    url: str,
    cache: JsonCache,
    limiter: RateLimiter,
    *,
    max_age_days: float | None,
    source_name: str,
) -> tuple[CachedResponse, list[str]]:
    """Cache-first fetch. `max_age_days=None` means the resource is immutable
    (a closed historical range): any cached copy is used without a live call.
    On a live failure the stale cache is returned, labeled, with a note."""
    notes: list[str] = []
    cached = cache.load(key)
    if cached is not None and (max_age_days is None or cached.age_days <= max_age_days):
        return cached, notes
    try:
        data = get_json(url, limiter, source_name=source_name)
    except ConnectorError as exc:
        if cached is not None:
            cached.stale = True
            notes.append(
                f"{source_name}: live refresh failed; using cached data "
                f"{cached.age_days:.1f} days old (stale). {exc}"
            )
            return cached, notes
        raise
    cache.store(key, data)
    return CachedResponse(data=data, fetched_utc=datetime.now(UTC)), notes
