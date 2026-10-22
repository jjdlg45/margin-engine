# External data sources (Run 4)

Every external source used by this project, verified before first use per the
Run 4 network rules. All sources are free and keyless. Raw downloads are never
committed; download scripts, mappings, checksums and fixtures are. Every test
passes offline using the recorded fixtures; live calls happen only in refresh
jobs and the optional `-m network` test group.

## UCI Machine Learning Repository — Online Retail II (Phase 28)

| Item | Value |
|---|---|
| Page | https://archive.ics.uci.edu/dataset/502/online+retail+ii |
| Data file | https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip (43.5 MB zip holding `online_retail_II.xlsx`, two sheets) |
| License | Creative Commons Attribution 4.0 International (CC BY 4.0) — verified on the dataset page 2026-10-21 |
| Citation | Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5CG6D |
| Contents | 1,067,371 invoice lines from a UK online wholesaler, 2009-12-01 to 2011-12-09, GBP; real returns/cancellations (invoices prefixed "C") |
| Rate limits | None documented; the download script fetches the file once and caches it outside the repo with a SHA-256 checksum |
| Used for | Phase 28 real-data case study |
| Fixture / sample | `data/sample_online_retail/` — a small derived sample committed under CC BY 4.0 with attribution (see the README beside it) |
| Download script | `scripts/download_online_retail.py` (downloads, checksums, extracts, converts to CSV with a deterministic per-row key, caches under the user cache dir) |

## Frankfurter — ECB reference exchange rates (Phases 28.4 / 29.1)

| Item | Value |
|---|---|
| Docs | https://frankfurter.dev/ |
| Endpoint | `https://api.frankfurter.dev/v1/{start}..{end}?base=GBP&symbols=USD` (time series; the newer `/v2/rates` exists, v1 remains supported) |
| Key | None required |
| Terms | Open source service; blended/reference rates free to use, commercial use permitted; caching daily rates locally is explicitly encouraged — verified on frankfurter.dev 2026-10-21 |
| Rate limits | No published quota; "rate limited to prevent abuse". Client behavior: one request per refresh window, local cache, exponential backoff on failure |
| Data | Daily reference rates (ECB-sourced), weekdays only |
| Used for | GBP -> USD conversion of the Online Retail II case study; daily FX refresh in live mode (Phase 30) |
| Fixture | `tests/fixtures/frankfurter_gbp_usd_2009_2011.json` — a recorded real response for offline tests and offline case-study runs |

## U.S. Bureau of Labor Statistics — Public Data API v1 (Phase 29.2)

To be verified before first use in Phase 29 (PPI series as external cost
index). Known constraint to respect: the keyless v1 API has a strict daily
query limit (25/day), so series are batched per request and cached
aggressively.

## USITC Harmonized Tariff Schedule (Phase 29.3, optional)

To be verified in Phase 29. If no free machine-readable endpoint is
available, a documented static tariff table is used instead (KNOWN GAP noted
at that point).
