# Margin Engine

**An auditable pricing and margin decision engine for B2B distributors.**
It ingests ERP invoice exports, reconciles every dollar to the cent, and shows where margin leaks, what each customer actually contributes, and how much evidence stands behind every number.

**[Live demo](https://jjdlg45.github.io/margin-engine/)**: a static site presenting the engine's results on a fixed-seed synthetic world.

![Margin Engine overview](docs/ui/review/overview-light-desktop.png)

Built for industrial, electrical, HVAC, plumbing, and foodservice distribution: any business with negotiated prices.

---

## The problem

Distributor pricing data is **messy** (returns, credit memos, duplicates, missing costs, mixed units) and **endogenous** (reps discount when demand is weak, so realized prices move with demand the data can't see). Tools that ignore the first produce numbers that don't reconcile. Tools that ignore the second recommend price increases based on elasticities biased toward zero.

This engine is built around three rules:

1. **Accounting first.** Every revenue, deduction, and cost reconciles to source totals exactly, in integer cents. Allocations use the largest-remainder method, so every line sums back to its source. No dollar is silently dropped, and nothing is forced onto lines to make a report look complete.
2. **Proven, not claimed.** A synthetic-data generator plants known truth (segments, elasticities, rebates, outliers, rep discounting bias, instruments), and the test suite measures recovery across random seeds.
3. **Fail closed.** When the data doesn't support a result, the engine says so (`insufficient_data`, `insufficient_precision`, `descriptive_elasticity`) instead of printing a confident-looking number.

## What it produces

| Output | Answers |
|---|---|
| Price waterfall: list → invoice → pocket → gross profit → contribution | Where is margin leaking: discounts, rebates, freight, returns? |
| Peer-group dispersion and outliers | Who pays far more or less than comparable customers for the same item? |
| Commercial segments | Which customer × product groups behave alike on price? |
| Price guidance with an economic floor | What's the reference band, and what's the lowest price that still clears minimum margin? |
| Price sensitivity with evidence status | How does volume respond to price, and is that estimate causal or only descriptive? |
| Opportunity sizing (three levels, never summed) | How much is recoverable at fixed volume vs. after the expected volume response? |
| Order and customer economics | What does each order and customer contribute after returns and cost-to-serve? |
| Cost and tariff pass-through scenarios | What happens to margin at 0–100% pass-through? |
| Action center: findings become proposed actions, reviewed maker-checker, exported to the ERP, then measured | Which findings became decisions — and did they actually work? |
| Robinson-Patman review flags with cost-to-serve evidence | Which recommended prices put likely-competing customers far apart on the same item? |

**Optional modules** switch on when their input data exists and say so when it doesn't: multi-currency, customer hierarchies and price-protected contracts, tiered retrospective rebates, kits and free goods, activity-based cost-to-serve, inventory costing (standard / average / FIFO / LIFO), key-value-item detection, quote win/loss curves, and cross-price effects within substitution groups. Analytic periods follow a configurable **fiscal calendar**: calendar months (default), 4-4-5 / 4-5-4 / 5-4-4 retail week calendars, or a custom period table — one declaration drives the sensitivity panel, peer-group quarters, baseline windows and the monthly GL variance check, while data-declared periods (rebate settlements, FX months) always stay as the source states them.

## Quickstart

```bash
uv sync

# Run on the committed sample data
uv run python -m margin_engine.cli run \
    --data data/sample/erp \
    --mapping config/mappings/synth_erp.yaml \
    --out output

# Open output/report.html. Tables are also in output/*.csv and *.parquet.
```

Generate a larger synthetic world (deterministic per seed):

```bash
uv run python -m synth.generate --out data/demo --seed 42
uv run python -m margin_engine.cli run --data data/demo/erp --mapping config/mappings/synth_erp.yaml --out output
```

Quality checks:

```bash
uv run ruff check .
uv run python -m mypy src
uv run python -m pytest
```

On Windows, run the dev tools as modules (`python -m pytest`, `python -m mypy`); Smart App Control blocks the unsigned shim executables.

### Web demo

The hosted site reads a JSON bundle exported by the engine:

```bash
uv run margin-engine export-web --demo --out web/public/data
cd web && npm install && npm run dev
```

`--demo` generates a fixed world (seed 3, 50 customers × 120 items, ~25k lines): the smallest scanned scale at which a segment reaches causal-supported elasticity under the Run 4 evidence criteria, so the site shows both the fail-closed and the supported path. CI rebuilds the bundle at deploy time; it is never committed. Every figure on the site comes from the engine's exports, and parity tests check them to the cent.

### Going live

The demo is static hosting, but the UI is already structured for a live
deployment behind a backend-for-frontend (BFF):

- **One data client, two adapters.** Every page reads through a single
  data-client interface ([`web/src/lib/api/types.ts`](web/src/lib/api/types.ts))
  using TanStack Query hooks - no component fetches JSON directly. The
  `static` adapter reads the exported files (GitHub Pages); the `http`
  adapter talks to the BFF. `VITE_DATA_SOURCE=http` selects the latter at
  build time ([`web/src/lib/api/client.ts`](web/src/lib/api/client.ts));
  anything else means static, so the hosted demo can never silently depend
  on a server.
- **The contract exists today.** [`docs/api/openapi.yaml`](docs/api/openapi.yaml)
  specifies the BFF: one aggregated endpoint per page plus paginated table
  endpoints (`page`, `page_size`, `sort`, named filters → `items`, `total`,
  `has_more`). The static adapter implements the same contract - filters,
  sorting and pagination behave identically, so swapping adapters changes
  transport, not behavior.
- **The BFF owns the server-side concerns:** response caching (results are
  immutable per engine run, so cache keys include the run manifest hash),
  rate limiting, pagination over large tables, and any server-side
  computation. The browser formats and filters; it never recalculates
  financials.
- **The export is already split** into per-page and per-table files, each
  under 1 MB, so the Overview loads only its own data and the other pages
  fetch lazily - the same request shapes the BFF would serve. Loading,
  error-with-Retry and partial-data states are handled at every section, with
  retries capped (two, exponential backoff, none on 4xx).

### Live mode

The BFF exists (Run 4, [`api/`](api/)): a FastAPI service implementing
[`docs/api/openapi.yaml`](docs/api/openapi.yaml) exactly, with engine outputs
and connector caches in DuckDB, ETag/Last-Modified response caching, inbound
per-client rate limiting (429 + Retry-After) and scheduled refreshes (FX
daily, PPI monthly) through the rate-limited [connectors](docs/data-sources.md)
— the API never calls an external source inside a request handler.

No free source publishes live B2B invoices, so live data is simulated
honestly with a **replay feed**: the Online Retail II dataset is fed in one
business day at a time, the engine re-runs, and the UI shows "Data as of
<day>" with the data labeled *historical data replayed*. Replay ticks are
idempotent (each day's world is filtered from source, never appended), so
re-running a day cannot double count.

Run everything locally with one command (Docker-free; see
[docs/deploy-api.md](docs/deploy-api.md) for hosting options):

```bash
uv run python scripts/live_mode.py          # UI :4174 in API mode, API :8321, replay on
```

### Run with Docker

One command starts the whole stack — a one-shot job exports the deterministic
demo bundle and action seeds, the API (with its in-process refresh scheduler)
serves it, and nginx serves the built UI with `/api` proxied same-origin:

```bash
docker compose up --build
# UI:  http://localhost:8080
# API: http://localhost:8000/api/health
```

State (the API's DuckDB stores, the action store with its review history, and
drift baselines) persists in the `state` volume across restarts; `docker
compose down -v` resets everything. Set `MARGIN_API_REPLAY: "1"` on the `api`
service in [`compose.yaml`](compose.yaml) to turn on the replay feed. CI
builds the images and smoke-tests the composed stack on every push
([`.github/workflows/docker.yml`](.github/workflows/docker.yml)) — GitHub's
runners have Docker even when the development machine does not.

## Action center

Analysis that nobody acts on is a report, not an engine. The Action center
closes the loop from finding to decision to measured outcome:

- **Generation.** The engine turns its own outputs into proposed actions —
  price changes, below-floor fixes, cost pass-through, service-policy reviews,
  rebate/discount reviews — each deterministic, with a plain-English rationale
  whose every number comes from the exports, an evidence status, and an
  expected impact at the *right* opportunity level for its type (levels are
  never summed across types). Guardrails are enforced at generation: no
  proposal below the binding floor, contract-protected pairs generate blocked
  actions that can never be approved, expected volume response only where the
  elasticity is causal-supported *and* the move sits inside identifying price
  support, and Robinson-Patman flags travel with the action.
- **Maker-checker review.** Lifecycle proposed → under review → approved /
  rejected → exported → implemented → measured, recorded as an append-only
  event log whose current status is derived by replay — never edited in place.
  Roles are analyst (proposes, edits, exports, implements), approver (approves
  or rejects — never an action they proposed themselves) and viewer. Approving
  a compliance-flagged action requires a written justification. Identity in
  local mode is a configured user name, documented everywhere as *not*
  authentication.
- **ERP price export.** `margin-engine export-prices` writes approved price
  actions as an ERP-importable price file (header names, date format, decimal
  separator and price unit configurable per deployment), deterministic and
  idempotent — re-exporting rewrites identical bytes and logs no duplicate
  events. A round-trip test re-imports the exported file through the engine's
  own ingest and matches every price to the cent.
- **Results tracking.** `margin-engine track` measures implemented actions
  after a configurable window: price realization (did the invoiced price
  actually move, or erode back?), volume against the prediction stored on the
  action at generation time — as a difference-in-differences against untreated
  same-segment pairs, so market trends never read as price effects — and
  margin before vs after. Every gate fails closed with its reason; calibration
  by evidence bucket appears only once enough actions are measured. On
  synthetic worlds with planted price interventions the tracker's pooled
  recovery error is 0.039 log points (see the validation table), and on a
  sparse world it measures nothing and says why.

The hosted demo runs the Action center as a **sandbox**: it starts from a
simulated, lifecycle-valid review history (labeled as such), and every
workflow button works — propose, edit, approve, reject, bulk approve, price
export and mark implemented — under the same guardrails as the API. Sandbox
changes live only in the visitor's browser (a banner says so, with a Reset
demo button and an analyst/approver identity switcher); business numbers
still come only from the engine's exports. In API/live mode the same
workflow runs server-side with the review history in DuckDB.

## Use it with your own data

The short path from a raw ERP export to results (the hosted demo also walks
through it on its "Use it with your own data" page):

1. **Install:** `uv sync` from a clone, or `docker compose up --build` for
   the full stack ([Run with Docker](#run-with-docker)).
2. **Map the export:** `margin-engine profile <file>` reports what each
   column looks like; `margin-engine draft-mapping <file>` writes a mapping
   draft in which every guess is marked `# CONFIRM` — the engine refuses to
   run until a person has confirmed each one
   ([Running on real data](#running-on-real-data)).
3. **Run:** `margin-engine run --data <file> --mapping <yaml> --out output`
   and open `output/report.html`; add `export-web` to browse results in the
   web app.
4. **Tie out to the books:** map an optional `gl_totals` table and the
   report compares engine monthly totals to the general ledger, flagging
   failing months instead of hiding them.
5. **Go live:** `uv run python scripts/live_mode.py` serves the UI in API
   mode with scheduled refreshes and drift flags ([Live mode](#live-mode)).
6. **Work the actions:** the [Action center](#action-center) turns findings
   into reviewed, exported, measured price actions.

## Supported input formats

The engine reads **tabular, line-level invoice exports**: one row per invoice line, as **CSV** or **.xlsx**. PDF and scanned invoices are not supported; they need a separate extraction step first.

Physical format is declared in the mapping, never guessed:

| Option | Default | Meaning |
|---|---|---|
| `delimiter` | `,` | CSV field separator: `,` `;` tab or `\|` |
| `encoding` | `utf-8` | Any Python codec (e.g. `windows-1252`); a UTF-8 BOM is always handled |
| `currency_symbols` | `[]` | Symbols stripped from money values, e.g. `["$"]`, `["MXN"]` |
| `negative_format` | `minus` | `minus` for `-113.57`, `parentheses` for `(113.57)` |
| `trim_whitespace` | `true` | Strip surrounding whitespace from headers and values |

Reference tables may also map optional display names - `customer_name` on the
customers table, and `item_description`, `supplier_name`, `category_name` on
the items table. When present, the demo UI and report show names first with
the ID secondary ("Gulf Coast Mechanical Supply · C10031") and search
matches either; when absent, IDs are shown alone.
| `decimal_separator` | `.` | `,` for `1.234,56`-style numbers |

Options set globally under `input:` can be overridden per file (`lines_input:` for the invoice lines, `input:` on any reference table), for example semicolon-delimited invoice lines alongside a comma-delimited price list. Blank rows are dropped and counted in the run manifest. A wrong declaration fails with a message naming the likely cause and the option to set.

## Running on real data

1. `margin-engine profile your_export.csv` scans the file and reports, per column: type guess, null rate, distinct count, detected date formats, and candidate canonical fields each with a confidence and a reason — plus the file-level delimiter, encoding, decimal separator and sign patterns.
2. `margin-engine draft-mapping your_export.csv` turns that into a mapping YAML in which **every guessed value is marked `# CONFIRM`** and listed under `unconfirmed_fields`. The engine refuses to run a mapping while that list is non-empty: a person confirms each guess by reviewing and deleting its entry. Suggestions never become silent decisions.
3. Declare the semantics that matter: the sign convention of every amount, whether discounts are embedded, the cost basis and what it includes — or declare their honest absence (`cost_unavailable: true` when the source genuinely has no cost; a prefix rule when the transaction type lives in the document number). Every reference table is optional; absent tables switch their features off and the report says so.
4. The engine refuses to compute anything whose meaning you didn't declare and tells you exactly which declaration is missing.

No code changes are needed for a new source system. Three more tools exist for real deployments:

- **Monthly GL variance check.** Map an optional `gl_totals` table (month, net sales, COGS, optionally returns) and the report compares engine period totals to the books: net sales and COGS separately, with signed and absolute variances, guarded variance percentages, the tolerance used, the fiscal-period and account mappings, unresolved reconciling items, and a line-to-GL trace sample. Variances outside tolerance are flagged, never hidden; without a GL file the check reports not testable instead of implying a pass. It is a variance control on top of the always-on, exact source-total reconciliation.
- **Validation without planted truth.** Real data has no ground truth, so every run includes checks that remain possible: a temporal backtest (fit on months 1..T−k, held-out volume-change prediction error vs a zero-elasticity baseline), placebo tests (instruments permuted in time must lose their reduced form and first stage; fake price-change dates must show no volume response), and rolling-window stability for both elasticities and segments. All informational — on the real case study below they correctly argue *against* causal claims.
- **Anonymizer.** `margin-engine anonymize` replaces customer, item and supplier IDs and names with salted keyed hashes (HMAC-SHA256; referential integrity preserved, so the same mapping runs on the copy) and can scale every money column by a secret salt-derived factor. The salt comes from a local file or environment variable and never appears in any output or manifest.

## Case study: Online Retail II (real data)

The engine runs end to end on a real public dataset: the UCI **Online Retail II** export of a UK online wholesaler — 1,067,371 invoice lines, 2009–2011, GBP, with real cancellations, 14.7% of revenue from anonymous customers, non-product fee codes and true duplicate rows, and **no cost, no list price, no instruments**. Every reconciliation check passes exactly (GBP kept per line, converted to USD at monthly ECB reference rates); cancellations classify as returns via a one-line mapping prefix rule; cost-dependent metrics report `cost_unavailable` instead of numbers; elasticity caps at `descriptive_elasticity`, and the no-planted-truth checks show why that gating is right (the backtest loses to a zero-elasticity baseline out of sample; fake-date placebos "detect" effects that cannot be real). Full write-up: [`docs/case-study-online-retail.md`](docs/case-study-online-retail.md); reproduce with [`scripts/download_online_retail.py`](scripts/download_online_retail.py).

## Validation results

Planted truth vs. recovered, on synthetic data. These figures are produced by `scripts/measure_validation.py`, which runs the actual measurements and writes `docs/validation.json`; the demo site reads the same file. No number is hand-typed.

| Check | Target | Measured |
|---|---|---|
| Accounting fixtures (hand-calculable cases) | Exact to the cent | All pass |
| Ledger reconciliation to source totals | Exact | Property suite passes on varied inputs |
| Rebate / freight recovery | Actuals exact; allocations within ±5% | Pass |
| Planted outlier detection | ≥ 90% recall, ≤ 5% false flags | 98.7% recall, 1.02% false flags |
| Commercial segments vs. planted (out-of-sample ARI) | ≥ 0.70 | 1.000 |
| Segment bootstrap stability (median ARI) | ≥ 0.80 | 1.000 |
| Elasticity recovery, planted-segment grain (5 seeds) | Median ≤ 0.10, P90 ≤ 0.15 | Median 0.019, P90 0.051, worst 0.110 |
| Elasticity recovery, end-to-end on engine segments (5 seeds) | Median ≤ 0.10, P90 ≤ 0.15 | Median 0.019, P90 0.072, worst 0.110 |
| 95% CI coverage (50 simulations, 300 estimates) | 90–98% | 92.0% |
| Naive estimator bias | Toward zero on every seed | 5/5 seeds, every segment |
| `weak_instrument` preset | Never causal-supported | Holds; weak-IV-robust (Anderson-Rubin) sets reported |
| `invalid_instrument` preset | Overidentification rejects ≥ 80% of seeds | Rejected and downgraded on 5/5 seeds |
| `price_constant` / `sparse` presets | Never above descriptive | Holds for every segment |
| Key-value-item candidates | ≥ 80% of planted KVIs found | 96.7% |
| Win-curve price coefficient, precision-gated segments | Within ±15% of planted | Worst 4.0% |
| 95% CI coverage under persistent (AR(1)) demand, shipped clustering | 90–98% | 93.3% (month × customer; the month-only and supplier × month alternatives cover 83.3% / 82.3% — reported, which is why they are not the default) |
| Determinism | Byte-identical outputs across runs | Byte-identical |
| Performance (core pipeline) | 500k+ lines under 2 minutes | 811,253 lines in 37.0s |
| Scenario matrix, full tier | Every invariant on every world (reconciliation, fail-closed, determinism, format invariance, monotonicity, no crash) | 300/300 worlds pass |
| Scale (10M lines under 15 min and 8 GB) | **KNOWN GAP** | Unverifiable on the 13.8 GB build machine (≈5 GB free): after the Run 5 scale refactor the largest run that fits is 241k lines in 13.0s at 2.64 GB peak (was 32.1s / 2.92 GB before the refactor; marginal memory ≈9.0 KB/line, was ≈10.0). Memory stays linear in lines across measured rungs, but that is an extrapolation — at ≈9 KB/line a full-featured 10M-line world would not fit 8 GB anywhere; the remaining lever (spilling the ledger to parquet between stages) is logged as the next scale step |
| Hybrid world: real BLS PPI series drive the cost instrument | Informational | Median abs elasticity error 0.037 over 6 segments; 4 causal-supported |
| Results tracker: planted +8% price interventions recovered (difference-in-differences) | Pooled error ≤ 0.10 log points; ≥ 50% of interventions measured | 21/30 measured; pooled recovery error 0.039 log points (a single pair's 90-day window carries ~0.26 log points of Poisson noise — reported, and why calibration aggregates instead of judging single actions) |
| Results tracker on a sparse world | Fail-closed: ≤ 25% of interventions get a number | 0/30 measured; every refusal carries its reason |

Reported but not gated, because the planted number is not the estimand once the model is misspecified: `realistic_noise` (median 0.051, P90 0.126, worst 0.210), `persistent_demand` (0.021), `heterogeneous_elasticity` (0.024 — the segment estimate recovers the within-segment average), `nonconstant_elasticity` (0.320 — a constant-elasticity model on curved demand reports a local average), and `competitor_comovement` (0.236, with **zero** segments reaching causal support: the Anderson-Rubin and robustness criteria catch what overidentification cannot). An honest indication that clean synthetic results overstate real-world accuracy.

## Method in brief

A mapping YAML declares column names and business meaning; ingestion fails actionably on anything undeclared. Classified transactions produce two views (clean sales observations and a complete financial ledger) and a staged waterfall with exact allocations. Prices normalize to an index against the effective-dated list price; robust z-scores flag data-quality anomalies and commercial outliers separately. Commercial segments come from a decision tree on the price index using context features only (guarded against leakage) and are checked for stability by bootstrap. Price guidance is historical reference bands against an exact economic margin floor, rounded up so it always clears minimum margin. Own-price elasticity is estimated per segment by fixed-effects instrumental-variables regression with two instruments (supplier cost index, category tariff rate), and first-stage, overidentification, robustness, and weak-IV-robust diagnostics feed a four-level evidence status. Opportunity is sized at three levels that are never added together. Findings become proposed actions with guardrails, reviewed maker-checker, exported to the ERP and measured after implementation. Full detail: [`docs/methodology.md`](docs/methodology.md).

## Honest labels and limitations

- **Historical reference bands are not optimal prices.** Targets stay `provisional_historical` unless elasticity evidence supports optimization.
- **Descriptive elasticities are not causal.** The naive estimate is reported to document bias, never used for decisions.
- **Instruments rest on assumptions.** The overidentification test only detects an invalid instrument when the two instruments disagree; exclusion restrictions are documented per instrument in `config/rules.yaml`.
- **Industry-wide cost shocks also move competitor prices.** A customer facing an industry-wide increase has nowhere cheaper to go, so measured responses are flatter than the response to a unilateral move: estimates **understate** own-price sensitivity, and overidentification cannot detect it because both instruments shift competitor prices in the same direction. Since Run 6 this is a **gate**, not a caveat: `causal_elasticity_supported` requires either an adequate public market price index (current, complete, varying within each period, at a grain matching the channel, and demonstrably related to prices) or an instrument whose competitor-price path is documented as absent with a written argument. Otherwise the status caps at an instrumented estimate, the estimand is reported as a market-wide cost-shock response with its bias direction, and the two-sided expected-change range is withheld. No configuration switch disables this. The gate still cannot discharge a violation the two instruments share through some other channel - that limitation is stated in the methodology and the status tooltip.
- **Volume responses are never extrapolated** beyond the price variation the instruments actually induced: proposed moves outside each segment's identifying price range are labeled `outside_price_support` and sized at unchanged volume.
- **Allocated and estimated amounts are labeled** as such, never presented as actuals. Unallocatable amounts stay visible as residuals with a plain-English explanation.
- **Item availability is proxied** by market-wide activity, since stockouts aren't observable in invoice data. The proxy is labeled.
- **Synthetic validation is not real-world validation.** Tests demonstrate recovery of planted truth; they don't prove accuracy on any specific company's data.

## Repository layout

| Path | Contents |
|---|---|
| `src/margin_engine/` | Engine: ingest, ledger, waterfall, analytics, models, report, web export |
| `synth/` | Synthetic world generator with planted ground truth |
| `config/` | Mappings (columns + semantics) and `rules.yaml` (policy and thresholds) |
| `tests/` | Accounting fixtures, property tests, input-format tests, recovery and coverage studies |
| `web/` | React + TypeScript demo site with Playwright and accessibility tests |
| `scripts/` | Validation measurement script |
| `docs/` | `methodology.md`, `validation.json`, design plan, and UI review screenshots |
| `data/sample/` | Small committed demo dataset |

## Author

**Juan Ignacio Jimenez**, Industrial Engineer · [LinkedIn](https://linkedin.com/in/juan-ignacio-jimenez)

## License

Copyright © 2026 Juan Ignacio Jimenez. All rights reserved.
This repository is shared for portfolio review. No license is granted to use, copy, modify, or distribute this code without written permission.