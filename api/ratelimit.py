"""Inbound rate limiting (30.3): token bucket per client, 429 + Retry-After.
Outbound limits live in the connectors (Phase 29); the API never calls an
external source inside a request handler - only scheduled refreshes do."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last: float


class TokenBucketLimiter:
    """requests_per_minute sustained, burst up to `burst` immediate."""

    def __init__(self, requests_per_minute: int = 120, burst: int = 30) -> None:
        self.rate = requests_per_minute / 60.0
        self.burst = float(burst)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> float | None:
        """None when allowed; otherwise seconds until the next token."""
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(client)
            if b is None:
                b = _Bucket(tokens=self.burst, last=now)
                self._buckets[client] = b
            b.tokens = min(self.burst, b.tokens + (now - b.last) * self.rate)
            b.last = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return None
            return max((1.0 - b.tokens) / self.rate, 0.1)
