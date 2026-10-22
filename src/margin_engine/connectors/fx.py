"""Frankfurter FX connector (Run 4, 29.1): daily reference exchange rates
(ECB-sourced), no API key. Terms and limits recorded in docs/data-sources.md.

The engine's fx_rates reference table is monthly (currency, period, rate =
reporting-currency units per unit of local currency). This connector fetches
DAILY rates and derives the monthly table using the month-end reference rate
(the last published daily rate of the month) - documented policy; monthly
averages would smooth over the engine's "rate in force for the invoice month"
semantics rather than approximate them.

Offline behavior: a closed historical range is immutable, so a cached or
fixture response is used without any live call; tests run entirely from the
recorded fixture (tests/fixtures/frankfurter_gbp_usd_2009_2011.json).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from margin_engine.connectors.base import (
    ConnectorError,
    JsonCache,
    RateLimiter,
    fetch_cached,
)

BASE_URL = "https://api.frankfurter.dev/v1"
SOURCE_NAME = "Frankfurter (frankfurter.dev, ECB reference rates)"

# One shared limiter: polite spacing, far under any plausible limit.
_LIMITER = RateLimiter(min_interval_s=1.0)


def fetch_daily_rates(
    base: str,
    quote: str,
    start: str,
    end: str,
    cache_dir: Path,
    *,
    fixture: Path | None = None,
    max_age_days: float | None = None,
) -> tuple[pl.DataFrame, list[str]]:
    """Daily rates quote-per-base for [start, end] (ISO dates), cache-first.

    `fixture` seeds the cache from a recorded response, so offline runs never
    attempt a live call. `max_age_days=None` treats the range as immutable
    history (no refresh); pass a number for ranges that end near today.
    Returns (DataFrame[date, rate], notes).
    """
    key = f"fx_{base}_{quote}_{start}_{end}".replace(":", "-")
    cache = JsonCache(cache_dir)
    if fixture is not None and cache.load(key) is None:
        if not fixture.exists():
            msg = f"FX fixture not found: {fixture}"
            raise ConnectorError(msg)
        import json

        payload = json.loads(fixture.read_text(encoding="utf-8"))
        cache.store(key, payload.get("data", payload))
    url = f"{BASE_URL}/{start}..{end}?base={base}&symbols={quote}"
    cached, notes = fetch_cached(
        key, url, cache, _LIMITER, max_age_days=max_age_days, source_name=SOURCE_NAME
    )
    rows = [
        {"date": d, "rate": float(rates[quote])}
        for d, rates in sorted(cached.data["rates"].items())
        if quote in rates
    ]
    if not rows:
        msg = f"{SOURCE_NAME}: no {quote}-per-{base} rates in [{start}, {end}]"
        raise ConnectorError(msg)
    df = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date("%Y-%m-%d"))
    if cached.stale:
        notes.append(f"FX rates are from a stale cache ({cached.age_days:.1f} days old)")
    return df, notes


def monthly_rates(daily: pl.DataFrame, currency: str) -> pl.DataFrame:
    """Engine fx_rates table from daily rates: month-end reference rate (the
    last published daily rate in each month)."""
    return (
        daily.sort("date")
        .with_columns(pl.col("date").dt.strftime("%Y-%m").alias("period"))
        .group_by("period", maintain_order=True)
        .agg(pl.col("rate").last().alias("rate"))
        .with_columns(pl.lit(currency).alias("currency"))
        .select("currency", "period", "rate")
        .sort("period")
    )


def write_fx_rates_csv(
    monthly: pl.DataFrame, out_path: Path, columns: dict[str, str] | None = None
) -> None:
    """Write the monthly table as a reference CSV for a mapping to declare.
    `columns` renames canonical -> source header (defaults keep canonical)."""
    df = monthly
    if columns:
        df = df.rename(columns)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_csv(out_path)
