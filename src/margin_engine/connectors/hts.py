"""USITC Harmonized Tariff Schedule connector (Run 4, 29.3): the free
machine-readable export at hts.usitc.gov/reststop (verified; terms and the
documented limitation recorded in docs/data-sources.md).

LIMITATION, stated up front: the endpoint serves the CURRENT revision only -
no history. A constant rate carries no identifying variation, so this source
feeds cost/tariff PASS-THROUGH SCENARIOS (incremental landed cost by
category), never the tariff instrument. The tariff instrument still requires
an effective-dated tariff_schedule table.
"""

from __future__ import annotations

import re
from pathlib import Path

import polars as pl

from margin_engine.connectors.base import ConnectorError, JsonCache, RateLimiter, fetch_cached

BASE_URL = "https://hts.usitc.gov/reststop/exportList"
SOURCE_NAME = "USITC Harmonized Tariff Schedule (current revision)"

_LIMITER = RateLimiter(min_interval_s=2.0)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def parse_general_rate(text: str) -> float | None:
    """'6.7%' -> 6.7; 'Free' -> 0.0; compound/specific rates ('2.5c/kg + 4%',
    '$1.50/kg') -> None (not a plain ad-valorem percentage - never guessed)."""
    t = (text or "").strip()
    if not t:
        return None
    if t.lower() == "free":
        return 0.0
    pcts = _PCT.findall(t)
    only_pct = _PCT.sub("", t).strip(" +") == ""
    if len(pcts) == 1 and only_pct:
        return float(pcts[0])
    return None


def fetch_tariff_rates(
    hts_from: str,
    hts_to: str,
    cache_dir: Path,
    *,
    fixture: Path | None = None,
    max_age_days: float = 30.0,
) -> tuple[pl.DataFrame, list[str]]:
    """Ad-valorem general rates for an HTS range: (htsno, description,
    general_rate_pct, rate_parsed). Compound/specific rates keep the raw text
    with rate_parsed=False."""
    key = f"hts_{hts_from}_{hts_to}"
    cache = JsonCache(cache_dir)
    if fixture is not None and cache.load(key) is None:
        if not fixture.exists():
            msg = f"HTS fixture not found: {fixture}"
            raise ConnectorError(msg)
        import json

        payload = json.loads(fixture.read_text(encoding="utf-8"))
        cache.store(key, payload.get("data", payload))
    url = f"{BASE_URL}?from={hts_from}&to={hts_to}&format=JSON&styles=false"
    cached, notes = fetch_cached(
        key, url, cache, _LIMITER, max_age_days=max_age_days, source_name=SOURCE_NAME
    )
    rows = []
    for rec in cached.data:
        htsno = str(rec.get("htsno") or "").strip()
        general = str(rec.get("general") or "").strip()
        if not htsno or not general:
            continue  # heading / grouping rows carry no rate
        rate = parse_general_rate(general)
        rows.append(
            {
                "htsno": htsno,
                "description": str(rec.get("description") or ""),
                "general_rate_raw": general,
                "general_rate_pct": rate,
                "rate_parsed": rate is not None,
            }
        )
    if not rows:
        msg = f"{SOURCE_NAME}: no rated lines in range {hts_from}-{hts_to}"
        raise ConnectorError(msg)
    df = pl.DataFrame(rows).sort("htsno")
    n_unparsed = df.filter(~pl.col("rate_parsed")).height
    if n_unparsed:
        notes.append(
            f"{SOURCE_NAME}: {n_unparsed} line(s) carry compound/specific rates "
            "that are not a plain percentage; kept as raw text, never guessed"
        )
    if cached.stale:
        notes.append(f"HTS data is from a stale cache ({cached.age_days:.1f} days old)")
    notes.append(
        "HTS serves the current revision only (no history): rates feed "
        "pass-through scenarios, never the tariff instrument"
    )
    return df, notes
