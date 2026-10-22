"""BLS Public Data API v1 connector (Run 4, 29.2): producer price indexes as
the external cost index for the instrument builder (27.6). Keyless; verified
limits recorded in docs/data-sources.md: 25 queries/day, 25 series/query,
10 years/query - so series are BATCHED per request and cached aggressively
(a closed historical range is never refetched).

The mapping from PPI series to item categories lives in config
(sensitivity.external_index_map); this module only fetches and shapes data.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import polars as pl

from margin_engine.connectors.base import ConnectorError, JsonCache, RateLimiter

ENDPOINT = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
SOURCE_NAME = "BLS Public Data API v1"
MAX_SERIES_PER_QUERY = 25
MAX_YEARS_PER_QUERY = 10

# Generous spacing: the binding constraint is 25 queries/DAY, enforced by
# batching + caching, not by per-second pacing.
_LIMITER = RateLimiter(min_interval_s=2.0)

_MONTH_PERIODS = {f"M{m:02d}": f"{m:02d}" for m in range(1, 13)}


def _post_json(payload: dict[str, object]) -> dict[str, object]:
    _LIMITER.wait()
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "margin-engine/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        msg = (
            f"{SOURCE_NAME} unreachable ({exc}). The daily limit is 25 queries; "
            "cached data is used when present, otherwise rerun later."
        )
        raise ConnectorError(msg) from exc


def fetch_ppi_series(
    series_ids: list[str],
    start_year: int,
    end_year: int,
    cache_dir: Path,
    *,
    fixture: Path | None = None,
    max_age_days: float | None = None,
) -> tuple[pl.DataFrame, list[str]]:
    """Monthly PPI levels: (series_id, period 'YYYY-MM', value), cache-first.

    One batched POST per call (<= 25 series, <= 10 years - enforced).
    `fixture` seeds the cache from a recorded real response for offline use.
    """
    if len(series_ids) > MAX_SERIES_PER_QUERY:
        msg = f"{len(series_ids)} series exceeds the v1 batch limit of {MAX_SERIES_PER_QUERY}"
        raise ConnectorError(msg)
    if end_year - start_year + 1 > MAX_YEARS_PER_QUERY:
        msg = (
            f"{end_year - start_year + 1} years exceeds the v1 limit of "
            f"{MAX_YEARS_PER_QUERY} per query; split the range"
        )
        raise ConnectorError(msg)
    key = f"bls_{'_'.join(sorted(series_ids))}_{start_year}_{end_year}"
    cache = JsonCache(cache_dir)
    if fixture is not None and cache.load(key) is None:
        if not fixture.exists():
            msg = f"BLS fixture not found: {fixture}"
            raise ConnectorError(msg)
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        cache.store(key, payload.get("data", payload))

    # fetch_cached expects a GET; BLS v1 needs a POST body, so reuse its
    # cache/stale semantics with a tiny adapter.
    notes: list[str] = []
    cached = cache.load(key)
    if cached is None or (max_age_days is not None and cached.age_days > max_age_days):
        try:
            data = _post_json(
                {
                    "seriesid": sorted(series_ids),
                    "startyear": str(start_year),
                    "endyear": str(end_year),
                }
            )
        except ConnectorError as exc:
            if cached is None:
                raise
            cached.stale = True
            notes.append(
                f"{SOURCE_NAME}: live refresh failed; using cached data "
                f"{cached.age_days:.1f} days old (stale). {exc}"
            )
            data = None
        if data is not None:
            status = str(data.get("status", ""))
            if status != "REQUEST_SUCCEEDED":
                msg = f"{SOURCE_NAME} returned status {status}: {data.get('message')}"
                raise ConnectorError(msg)
            cache.store(key, data)
            cached = cache.load(key)
    assert cached is not None

    rows = []
    payload_data = cached.data
    for series in payload_data.get("Results", {}).get("series", []):
        sid = series["seriesID"]
        for obs in series.get("data", []):
            period = obs.get("period", "")
            if period not in _MONTH_PERIODS:  # skip annual (M13) and quarterly
                continue
            rows.append(
                {
                    "series_id": sid,
                    "period": f"{obs['year']}-{_MONTH_PERIODS[period]}",
                    "value": float(obs["value"]),
                }
            )
    if not rows:
        msg = f"{SOURCE_NAME}: no monthly observations for {series_ids} in {start_year}-{end_year}"
        raise ConnectorError(msg)
    df = pl.DataFrame(rows).sort("series_id", "period")
    if cached.stale:
        notes.append(f"PPI data is from a stale cache ({cached.age_days:.1f} days old)")
    missing = sorted(set(series_ids) - set(df["series_id"].unique().to_list()))
    if missing:
        notes.append(f"{SOURCE_NAME}: no data returned for series {missing}")
    return df, notes


def write_external_cost_series_csv(df: pl.DataFrame, out_path: Path) -> None:
    """Write the engine's external_cost_series reference table (canonical
    columns series_id, period, value - see 27.6)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.select("series_id", "period", "value").write_csv(out_path)
