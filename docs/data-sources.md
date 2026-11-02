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

| Item | Value |
|---|---|
| Docs | https://www.bls.gov/developers/ (signatures: /developers/api_signature.htm; limits: /developers/api_faqs.htm) |
| Endpoint | `POST https://api.bls.gov/publicAPI/v1/timeseries/data/` with `{seriesid: [...], startyear, endyear}` |
| Key | None (v1 is open; v2 requires registration) |
| Limits (verified 2026-10-22) | v1 unregistered: **25 queries/day, 25 series/query, 10 years/query**. Client behavior: all series batched into one request, closed historical ranges cached permanently, 2 s spacing, backoff on failure |
| Data | BLS time series (public domain); used series: PPI commodities WPU101 (iron & steel), WPU081 (lumber), WPU0638 (plastic resins) |
| Used for | External cost index option of the instrument builder (27.6, `sensitivity.instrument_source: external`) and the 29.4 hybrid world |
| Fixture | `tests/fixtures/bls_ppi_2016_2025.json` — a recorded real response (3 series x 120 months) |

## USITC Harmonized Tariff Schedule (Phase 29.3)

| Item | Value |
|---|---|
| Site | https://hts.usitc.gov/ |
| Endpoint (verified working 2026-10-22) | `GET https://hts.usitc.gov/reststop/exportList?from=<hts>&to=<hts>&format=JSON` — the site's own machine-readable export (also offered as CSV/Excel from the search UI) |
| Key / terms | None; US government work (public domain). No published rate limit — client uses 2 s spacing, 30-day cache |
| **Documented limitation** | The endpoint serves the **current revision only — no history**. A constant rate has no identifying variation, so HTS rates feed cost/tariff **pass-through scenarios**, never the tariff instrument (which still requires an effective-dated tariff_schedule table). Compound/specific rates ("2.5c/kg + 4%") are kept as raw text, never coerced to a percentage |
| Fixture | `tests/fixtures/hts_8501_8516.json` — a recorded real export (440 rows, HTS 8501-8516) |

## Series kinds and the role each one may play (Run 6, Phase 80)

Every external series mapped into the engine declares a `provenance` block
(provider, url, retrieved_utc, license, update_cadence, coverage,
aggregation_level, series_kind). The kind is not documentation - it decides
what the series is allowed to do.

| Series kind | Example | May be used as | Refused as |
|---|---|---|---|
| `cost_index` | BLS producer price index | the supplier cost instrument | the competitor-price control |
| `selling_price_index` | a published market price index | the competitor-price control | an instrument |
| `other` | anything else | neither role | both |

The two refusals are not symmetric pedantry. A cost index used as a
competitor-price control would look like a control and would not be one: the
same cost shocks that identify the elasticity also move it. A selling-price
series used as an instrument would be the channel the exclusion restriction
has to rule out - and feeding competitors' prices into a pricing model is the
antitrust concern itself, not a modelling nicety.

## Intuit QuickBooks Online Accounting API (Run 7, Phase 92)

| Item | Value |
|---|---|
| Docs | https://developer.intuit.com/app/developer/qbo/docs/get-started |
| Base URLs | sandbox `https://sandbox-quickbooks.api.intuit.com`, production `https://quickbooks.api.intuit.com`; token endpoint `https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer` |
| Auth | OAuth 2.0 authorization-code with refresh tokens; scope `com.intuit.quickbooks.accounting` (used read-only: queries and reports only). Intuit rotates refresh tokens; the connector persists the newest one in a local token cache (never committed, never in any output) |
| API version | Minor version pinned to **75** - the only supported one since 2025-08-01, when versions 1-74 were retired (verified 2026-11-02 against Intuit's announcement and ecosystem notes: codat.io update 250219, python-quickbooks PR #381) |
| Rate limits (verified 2026-11-02, developer.intuit.com/.../limits-and-throttles) | 500 requests/min per realm, 10 concurrent per app+realm; HTTP 429 with a 60 s wait advised. Client limiter: 0.2 s spacing, 429 handled with a 60 s wait and retry |
| Terms | Intuit Developer terms; sandbox companies are free developer resources intended for integration testing |
| Status | **Built against the documented API, not yet verified live** - live verification awaits the owner's Intuit developer app and sandbox company |
| Fixtures | `tests/test_quickbooks.py` - documented-schema fixtures, labeled as such (to be replaced by recorded sandbox responses once credentials exist) |

## HubSpot CRM v3 API (Run 7, Phase 93)

| Item | Value |
|---|---|
| Docs | https://developers.hubspot.com/docs - CRM v3 objects (companies, deals, line items), pipelines, search |
| Base URL | `https://api.hubapi.com` |
| Auth | Private app token (Bearer), read-only scopes: `crm.objects.companies.read`, `crm.objects.deals.read`, `crm.objects.line_items.read`, `crm.objects.quotes.read` |
| Rate limits (verified 2026-11-02, developers.hubspot.com usage guidelines) | private apps ~100 requests/10 s on Free/Starter (190 on Pro/Enterprise) plus daily caps by tier; 429 carries `Retry-After`. Client limiter: 0.15 s spacing, honors `Retry-After` |
| Terms | HubSpot developer terms; free developer test accounts are intended for integration testing |
| Status | **Built against the documented API, not yet verified live** - live verification awaits the owner's developer test account and private app token |
| Fixtures | `tests/test_hubspot.py` - documented-schema fixtures, labeled as such |
| Note | Won/lost is read from each pipeline's own stage metadata (`isClosed`, `probability`), never guessed from stage names; only closed-won/closed-lost deals become quote outcomes (open, stage-unknown, company-less, line-item-less deals are excluded with recorded reasons) |
