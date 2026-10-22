# RUN 4 — Correct, real-data-ready, and live

Owner instruction: re-read CLAUDE.md from disk, then append this file's content to CLAUDE.md as section "RUN 4" and commit (`docs: Run 4 spec`). Then execute Run 4 end to end.

---

## 0. Run mode and rules for this run

- **Branch:** do all Run 4 work on a new branch `run4`, created from `main`. Never commit Run 4 work to `main` and never push. The public demo deploys from `main`, so it stays untouched until the owner reviews and merges.
- **Continuous run.** Execute every phase below in order without waiting for approval. There is no hard stop between phases; stop only on a stop condition.
- **After every phase:** run the Python checks (ruff, ruff format, mypy, full pytest) and the web checks (tsc, vitest, build, Playwright) that apply, **confirm each exit code**, append a phase report to `docs/build-log.md` (what changed, tests added, before/after numbers, decisions, known gaps), and make one commit `phase-N: <summary>`.
- **Stop conditions** (stop, write the reason in the build log, wait for the owner):
  - A check still fails after 3 fix attempts.
  - An existing validation target in section 8 of CLAUDE.md or `docs/validation.json` fails after a change. Report the actual result; never loosen a target.
  - The spec doesn't answer a business-logic question.
- **Network rules:** external downloads and APIs are free and keyless. Before first use, verify each source's current URL, license or terms, and rate limits, and record them in `docs/data-sources.md`. If a source is unavailable, or its terms forbid this use, log it as a KNOWN GAP, use recorded fixtures instead, and continue. **Every test must pass offline** using recorded responses; live calls are only for the refresh jobs and an optional `@pytest.mark.network` test group.
- **Data rules:** never commit downloaded raw datasets. Commit download scripts, mappings, checksums, and at most small derived samples where the license allows (note the license next to the sample). No real company data, no secrets, no API keys in the repo.
- **Resuming:** if a session is interrupted, read `docs/build-log.md` and continue from the last completed phase.

---

## Phase 24 — Logic remediation

Consolidates two independent logic audits; every item was checked against the current code.

**24.1 Pocket-aware opportunity and optimal price.** `opportunity/sizing.py` computes contribution as (P − C)·Q on invoice-basis prices, ignoring rebates and concessions that scale with price. Use contribution per unit = P·(1 − d) − F − C, with d and F from guidance. Optimal price P\* = (C + F)/(1 − d) × β/(1 + β), clipped as before. Rename "proposed pocket price" variables to match their actual basis. Hand-calculable fixtures.

**24.2 Missing cost is never zero.** Remove `fill_null(0)` on unit cost in opportunity math; cost-less pairs are excluded from cost-dependent levels and counted as `cost_unavailable`. Test.

**24.3 Explicit margin definitions and a contribution floor.** Rename the config key to `min_pocket_gross_margin`. Add optional `min_contribution_margin` with floor = ((C + K)/(1 − m_c) + F)/(1 − d), K = per-unit cost-to-serve (labeled estimated when from config rates). Guidance reports both floors and which binds. New status `market_reference_below_floor` when segment P50 sits below the binding floor. Fixtures.

**24.4 Units in documentation.** The code already compares in index space (floor_index = invoice floor ÷ list), which is correct. Make every methodology formula state its unit. Test that index-space and dollar-space targets agree for valid list prices. Fallback list prices carry provenance and are excluded from floor-index comparison when the list is missing, zero, or stale.

**24.5 Inference under persistent demand.** Add AR(1) customer demand shocks to the generator (`persistent_demand` preset, configurable persistence). Add two-way clustering (month × customer) as a `cluster_level` option. Re-run the 50-simulation coverage study under persistent demand for each option, record results in validation.json, and default to whichever keeps coverage within 90–98%.

**24.6 Extrapolation guard.** Record each segment's identifying price range (5th–95th percentile of instrument-predicted log-price changes). Expected-change and optimized prices are produced only when the proposed move lies within that range; otherwise fixed-volume only, status `outside_price_support`. Report how many records change status.

**24.7 Causal threshold.** Keep F ≥ 10 for `iv_estimated_elasticity`. For `causal_elasticity_supported`, also require the Anderson-Rubin set to be bounded and no wider than 1.5× the primary interval. Report status changes on demo and validation worlds.

**24.8 Zero rows and carried-forward prices.** Add a robustness estimate on positive-quantity rows only and include its gap in the robustness-spread criterion. Report the share of carried-forward rows per segment.

**24.9 Misspecified validation worlds (informational, not gated).** Presets for elasticity varying within segment, non-constant elasticity, and competitor prices co-moving with cost shocks. Report recovery and status outcomes in validation.json and the README.

**24.10 Competitor co-movement.** Optional market/competitor price index as an elasticity control. Document in limitations that industry-wide cost shocks also move competitor prices, so estimates likely understate own-price sensitivity, and that overidentification can't detect it.

**24.11 GAAP alignment and labels.** Rename the "net revenue" KPI to "net invoiced revenue" everywhere; note that pocket revenue approximates GAAP net sales. Add a methodology section mapping stages to ASC 606 (variable consideration, consideration payable to a customer, right of return), ASC 330, ASC 705-20 and the shipping-and-handling policy election, stating where the management view differs (returns recognized when they occur; freight below gross profit).

**24.12 LIFO** costing method, with fixtures.

**24.13 Opportunity framing.** Add a conservative level-2 variant (below-P25 pairs to P25) alongside the existing one. Win-aware optimization uses only one volume-response source per record (enforce in code); document the win-curve/PPML overlap.

**24.14 Config hygiene.** Move the hardcoded `_pair_n >= 6` rule in `panel/build.py` to config.

---

## Phase 25 — Filters, lookups, and names

**25.1 Names in the data model.** Optional canonical fields `customer_name`, `item_description`, `supplier_name`, `category_name` in mapping and schema (absent → IDs only, stated in the UI). The generator creates realistic deterministic names (distributor-style customer names, item descriptions with size and material, supplier and category names). Display name first with ID secondary. Search matches name or ID.

**25.2 One shared filter system.** React Aria Components for comboboxes, multi-selects, and range inputs, styled with project tokens; no native `<select>` anywhere. Searchable combobox with type-ahead and a Name/ID lookup toggle. Filter bar: labeled filters, removable chips, Clear all, result count ("Showing 38 of 543"), all state in the URL. Range filters with min/max and presets.

**25.3 Per page.**
- Price guidance: a filterable table of pairs (customer, item, segment, status multi-select, current price % of list range, gap to floor); clicking a row opens the band chart; deep links to one pair still work.
- Top opportunities: customer, item, evidence status, volume treatment, gross opportunity range, % below target range.
- Customers & orders: customer, service flags, losing-money toggle, returns impact range, % revenue excluded range, order count range.
- Scenarios: searchable combobox grouped by scope type with pair counts.

**25.4 Verify.** Playwright for each filter alone and combined, chips, clear all, URL round trip, keyboard-only use, name and ID search, range boundaries; filtered counts match the data (parity). axe clean, raw-code scanner clean.

---

## Phase 26 — Live-ready UI data layer

**26.1** All pages fetch through one data client using TanStack Query hooks; no component reads data files directly. Two adapters behind one interface: `static` (files, for GitHub Pages) and `http` (the API in Phase 30), selected by `VITE_DATA_SOURCE`.

**26.2** Write the OpenAPI spec in `docs/api/openapi.yaml`: one aggregated endpoint per page, plus paginated table endpoints (page, page_size, sort, filters → items, total, has_more). The static adapter implements this same contract locally.

**26.3** Split the bundle: per-page and per-table files, each under 1 MB (target under 300 KB). The Overview loads only its own data.

**26.4** Loading and error states: skeletons per section; error state with plain-English message and Retry; error boundaries per route and major section; retries capped (max 2, exponential backoff, none on 4xx); never render partial data as complete.

**26.5** TanStack Virtual for any table or list over 100 rows, including combobox options.

**26.6 Verify.** Playwright with network interception: slow responses show skeletons; failures show the error state; Retry recovers; retries stop at the cap; every data file under 1 MB; virtualized tables render only visible rows.

---

## Phase 27 — Real-data readiness

**27.1 Data profiler and mapping assistant.** `margin-engine profile <file>` scans a CSV/.xlsx export and reports column types, null rates, distinct counts, candidate canonical fields with a confidence and reason, detected date formats, delimiter, encoding, decimal separator, sign patterns, and suspected transaction-type codes. `margin-engine draft-mapping <file>` writes a draft mapping YAML with every guessed field marked `# CONFIRM`. The engine refuses to run a mapping that still has unconfirmed fields. Suggestions never become silent decisions.

**27.2 Scale.** Run the full pipeline on a generated 10M-line world. If peak memory exceeds 8 GB or runtime exceeds 15 minutes, move the heavy stages to DuckDB-backed or streaming processing until both pass. Record runtime and peak memory in validation.json.

**27.3 Books tie-out.** Optional input `gl_totals.csv` (month, net sales, COGS, optionally returns). A tie-out report compares engine monthly totals to the books with absolute and % differences and a configurable tolerance. Failing months are flagged, never hidden.

**27.4 Validation without planted truth.**
- Temporal backtest: fit elasticity on months 1..T−k, predict volume response to observed price changes in later months, report error against a naive baseline.
- Placebo tests: random fake price-change dates and a shuffled instrument should show no effect; report the placebo distribution.
- Stability: segment and elasticity stability across rolling time windows.
Report all of this in a "Real-data validation" section of the report.

**27.5 Anonymizer.** `margin-engine anonymize` hashes customer, item, supplier IDs and names with a salted keyed hash (salt from a local file or env var, never committed) and optionally scales money by a secret factor, so results can be shared without exposing data. The salt never appears in outputs or the manifest.

**27.6 Instrument builder.** Build the two instruments from data a real company has, instead of ready-made shock columns:
- Supplier cost index from purchase or receipt history (log change in average purchase cost per supplier per month, demeaned).
- Category tariff index from an optional tariff-rate table by category and month.
- An external index option (Phase 29) mapped to categories by a config table.
Document each instrument's exclusion rationale in config as before.

**27.7 Scenario matrix (invariant testing).** Generate many worlds automatically and check rules that must hold without knowing the true answer.
- **Dimensions** (combinatorial, sampled with a fixed seed so the matrix is deterministic): world size (tiny to large), sparsity, missing-cost rate (0–50%), returns rate, credit-memo rate, currency mix (single / multi), ERP format profile (delimiter, encoding, currency symbols, parentheses negatives, BOM, padded whitespace, blank rows, .xlsx), optional tables present or absent, and demand assumptions (every preset including `persistent_demand` and the misspecified presets from 24.9).
- **Invariants checked on every world:**
  - Reconciliation is exact to the cent; every allocation sums to its source.
  - No crash: invalid input fails with an actionable message, never a raw stack trace.
  - Fail-closed: whenever the planted truth is not identifiable (weak or invalid instrument, constant prices, sparse data, misspecified demand), no segment reaches `causal_elasticity_supported`.
  - Determinism: running the same world twice gives byte-identical exports.
  - Format invariance: the same economic data in different ERP format profiles gives identical ledger totals.
  - Monotonicity sanity: raising the minimum margin never lowers any floor; adding a cost increase never raises pocket gross profit at fixed price.
- **Two tiers:** a fast tier (around 30 worlds, small sizes) in the default test suite and CI; a full tier (several hundred worlds) behind `@pytest.mark.slow`, run once in this phase with results summarized in validation.json (worlds run, invariant pass counts, any failures with the world's parameters so they can be reproduced).
- Any invariant failure is a bug to fix in this phase, not a target to relax. If a failure can't be fixed after 3 attempts, stop per the stop conditions.

---

## Phase 28 — Real dataset case study (free, public)

**28.1 Dataset.** UCI Machine Learning Repository "Online Retail II": real invoice lines from a UK online wholesale business, about 1M rows over 2009–2011, with real returns and cancellations (invoices prefixed "C"), in GBP. Verify the current URL and license (expected CC BY 4.0) and record them in `docs/data-sources.md`. Write `scripts/download_online_retail.py` that downloads, checksums, and caches it outside the repo.

**28.2 Mapping.** Write `config/mappings/online_retail_ii.yaml` using the profiler (27.1) as the starting point, with every field confirmed by reasoning in comments. The dataset has **no cost and no list price**. Handle that honestly: cost-dependent metrics report `cost_unavailable`; price indexes use the item-median fallback with its provenance label; no floor, contribution or margin numbers are invented.

**28.3 Run and document.** Run the full pipeline. Write `docs/case-study-online-retail.md`: data issues found (cancellations, negative quantities, missing customer IDs, non-product codes like postage and manual adjustments, duplicates), how each was classified, the reconciliation result, which stages ran and which correctly switched off, dispersion and segmentation findings, elasticity status (expected to be descriptive at most, since the data has no instruments), and lessons for real deployments.

**28.4 Multi-currency.** The data is in GBP. Use the FX connector (29.1) to convert to USD reporting currency at historical daily rates, with a fixture for offline tests.

**28.5 Committed sample.** If the license allows, commit a small derived sample (≤ 5,000 lines) for tests and the case-study example, with attribution and license noted beside it. Otherwise generate a synthetic sample with the same schema.

---

## Phase 29 — External data connectors (free, keyless)

Every connector: verify current endpoint, terms, and limits before use and record them; cache responses locally (DuckDB or files) with timestamps; respect the documented rate limits with a client-side limiter and exponential backoff; never call the same resource more than once per refresh window; record a fixture of real responses for offline tests; fail with an actionable message and fall back to the last cached value, labeled stale with its age.

**29.1 FX rates:** Frankfurter (European Central Bank reference rates, no key). Daily rates for the reporting currency.

**29.2 Producer price indexes:** U.S. Bureau of Labor Statistics Public Data API, version 1 (no key; strict daily query limit, so batch series per request and cache aggressively). Pull PPI series relevant to distribution categories as the external cost index for 27.6, mapped to categories by a config table.

**29.3 Tariff rates (optional):** the USITC Harmonized Tariff Schedule data. Verify whether a free machine-readable endpoint is available; if not, log a KNOWN GAP and use a documented static tariff table.

**29.4 Hybrid demonstration world.** A generator option `--external-cost-index` that drives supplier cost paths from the real PPI series (via cache or fixture) instead of simulated shocks, so the instrument pipeline runs on real-world cost movements. Report elasticity recovery on this world in validation.json (informational).

---

## Phase 30 — Backend-for-frontend API and live mode

**30.1 Service.** A FastAPI service in `api/` implementing `docs/api/openapi.yaml` from Phase 26 exactly: one aggregated endpoint per page plus paginated, filterable, sortable table endpoints; responses under 1 MB per call. Heavy computation happens server-side; the browser never recalculates financials. Health and version endpoints. You may consult the local `archive/old-run3-api` branch for reusable pieces, but build to this spec.

**30.2 Storage and caching.** Engine outputs and connector caches live in DuckDB. Responses carry ETag/Last-Modified; the API caches computed responses until the next refresh.

**30.3 Rate limiting.** Inbound: per-client limits with 429 and Retry-After. Outbound: all connector calls go through the Phase 29 limiter; the API never calls an external source per user request, only in scheduled refreshes.

**30.4 Live mode with a replay feed.** No free source publishes live B2B invoices, so live invoices are simulated honestly: a replay job feeds the Online Retail II data into the system one business day at a time on a schedule (configurable speed, e.g. one data-day per minute for demos), appending to the ledger. Scheduled refreshes: FX daily, PPI monthly, engine incremental or full re-run after each replay batch. The UI shows "Data as of …" and refresh status for every section, and labels replayed data as historical data replayed.

**30.5 UI in API mode.** `VITE_DATA_SOURCE=http` points the Phase 26 data layer at the API. Loading, error, retry, pagination, and virtualization all work against real network calls.

**30.6 Run locally with one command.** `docker compose up` (or a documented `uv` + `npm` script if Docker is unavailable) starts the API, the refresh scheduler, and the UI in API mode. Document it in the README under "Live mode."

**30.7 Verify.** API contract tests generated from the OpenAPI spec; parity tests (API responses equal engine outputs to the cent); rate-limit tests (inbound 429, outbound limiter honored); refresh-job tests with fixtures; replay-job idempotency (re-running a day doesn't double count); Playwright e2e in API mode including a simulated upstream failure that shows the stale-data label.

**30.8 Deployment (documented, not executed).** Write `docs/deploy-api.md` describing how the owner could host the API on a free tier (verify current free-tier terms of at least two providers and note limits), with environment variables, the scheduler, and how the hosted UI would switch to API mode. Do not create accounts or deploy. The GitHub Pages demo stays static.

---

## Phase 31 — Documentation and final report

- README: add "Real data" (profiler, mapping, tie-out, validation without planted truth, anonymizer), "Case study: Online Retail II" (link), "Live mode" (one-command local run, replay feed, connectors, rate limits), and update the validation table from validation.json. Keep the existing tone; no "How it was built" section.
- `docs/data-sources.md`: every external source with URL, license or terms, rate limits, what it's used for, and fixture location.
- `docs/run4-report.md`: every phase, what changed, tests added, before/after numbers (opportunity totals, status counts, coverage, runtime and memory), known gaps with reasons, and anything the owner must decide.
- Final commit on `run4`, then **stop**. Do not merge, do not push.
