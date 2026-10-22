"""Run 4 (29.1 / 28.4): FX connector. Every test here runs OFFLINE from the
recorded fixture; the one live call is behind the `network` marker."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import margin_engine.connectors.base as base
from margin_engine.connectors.base import (
    ConnectorError,
    JsonCache,
    RateLimiter,
    fetch_cached,
)
from margin_engine.connectors.fx import fetch_daily_rates, monthly_rates

FIXTURE = Path(__file__).parent / "fixtures" / "frankfurter_gbp_usd_2009_2011.json"


def test_fixture_rates_load_offline(tmp_path: Path) -> None:
    daily, notes = fetch_daily_rates(
        "GBP", "USD", "2009-12-01", "2011-12-09", tmp_path, fixture=FIXTURE
    )
    assert daily.height > 500  # ~523 ECB business days
    assert daily["date"].is_sorted()
    assert (daily["rate"] > 1.0).all() and (daily["rate"] < 2.5).all()  # GBP/USD sanity
    assert notes == []
    # Second call: served from the cache (still no network).
    again, _ = fetch_daily_rates(
        "GBP", "USD", "2009-12-01", "2011-12-09", tmp_path, fixture=FIXTURE
    )
    assert again.equals(daily)


def test_monthly_rates_are_month_end() -> None:
    import polars as pl

    daily = pl.DataFrame(
        {
            "date": pl.Series(["2010-01-04", "2010-01-29", "2010-02-01", "2010-02-26"]).str.to_date(
                "%Y-%m-%d"
            ),
            "rate": [1.60, 1.62, 1.55, 1.53],
        }
    )
    m = monthly_rates(daily, "GBP")
    assert m.to_dicts() == [
        {"currency": "GBP", "period": "2010-01", "rate": 1.62},
        {"currency": "GBP", "period": "2010-02", "rate": 1.53},
    ]


def test_stale_fallback_with_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = JsonCache(tmp_path)
    cache.store("k", {"x": 1})
    # Age the cache artificially.
    import json

    p = tmp_path / "k.json"
    payload = json.loads(p.read_text(encoding="utf-8"))
    payload["fetched_utc"] = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
    p.write_text(json.dumps(payload), encoding="utf-8")

    def boom(*a: object, **k: object) -> None:
        raise ConnectorError("down")

    monkeypatch.setattr(base, "get_json", boom)
    res, notes = fetch_cached(
        "k",
        "https://example.invalid/x",
        cache,
        RateLimiter(0.0),
        max_age_days=1.0,
        source_name="test source",
    )
    assert res.data == {"x": 1}
    assert res.stale is True
    assert any("stale" in n for n in notes)


def test_no_cache_and_no_network_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise ConnectorError("test source unavailable after 3 attempts (down)")

    monkeypatch.setattr(base, "get_json", boom)
    with pytest.raises(ConnectorError, match="unavailable"):
        fetch_cached(
            "nope",
            "https://example.invalid/x",
            JsonCache(tmp_path),
            RateLimiter(0.0),
            max_age_days=1.0,
            source_name="test source",
        )


def test_rate_limiter_spaces_calls() -> None:
    lim = RateLimiter(min_interval_s=0.08)
    lim.wait()
    t0 = time.monotonic()
    lim.wait()
    assert time.monotonic() - t0 >= 0.07


@pytest.mark.network
def test_live_fetch_small_range(tmp_path: Path) -> None:
    daily, _ = fetch_daily_rates("GBP", "USD", "2024-01-02", "2024-01-05", tmp_path)
    assert daily.height >= 3
