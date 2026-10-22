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

**Optional modules** switch on when their input data exists and say so when it doesn't: multi-currency, customer hierarchies and price-protected contracts, tiered retrospective rebates, kits and free goods, activity-based cost-to-serve, inventory costing (standard / average / FIFO), key-value-item detection, quote win/loss curves, and cross-price effects within substitution groups.

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

- **One data client.** Every page reads through a single data-client
  interface ([`web/src/lib/api/types.ts`](web/src/lib/api/types.ts)) using
  TanStack Query hooks - no component fetches JSON directly. The static
  adapter implements that interface over the exported files; a live
  deployment swaps in an HTTP adapter against the BFF, a one-file change in
  [`web/src/lib/api/client.ts`](web/src/lib/api/client.ts).
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

1. Copy `config/mappings/synth_erp.yaml` and edit it for your export: column names, date formats, transaction codes, and the semantics that matter: the sign convention of every amount, whether discounts are embedded, and the cost basis and what it includes.
2. Point `--data` at the folder with your invoice export and any reference tables (price list, rebate programs and settlements, order charges, freight log, UOM conversions, supplier cost index, tariff schedule, quotes, receipts). Every table is optional; absent tables switch their features off and the report says so.
3. The engine refuses to compute anything whose meaning you didn't declare and tells you exactly which declaration is missing.

No code changes are needed for a new source system.

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
| Determinism | Byte-identical outputs across runs | Byte-identical |
| Performance (core pipeline) | 500k+ lines under 2 minutes | 811,253 lines in 32.2s |

A `realistic_noise` preset (Poisson count quantities, higher noise) is reported but not gated: median error 0.051, P90 0.126, worst 0.210. It's an honest indication that clean synthetic results overstate real-world accuracy.

## Method in brief

A mapping YAML declares column names and business meaning; ingestion fails actionably on anything undeclared. Classified transactions produce two views (clean sales observations and a complete financial ledger) and a staged waterfall with exact allocations. Prices normalize to an index against the effective-dated list price; robust z-scores flag data-quality anomalies and commercial outliers separately. Commercial segments come from a decision tree on the price index using context features only (guarded against leakage) and are checked for stability by bootstrap. Price guidance is historical reference bands against an exact economic margin floor, rounded up so it always clears minimum margin. Own-price elasticity is estimated per segment by fixed-effects instrumental-variables regression with two instruments (supplier cost index, category tariff rate), and first-stage, overidentification, robustness, and weak-IV-robust diagnostics feed a four-level evidence status. Opportunity is sized at three levels that are never added together. Full detail: [`docs/methodology.md`](docs/methodology.md).

## Honest labels and limitations

- **Historical reference bands are not optimal prices.** Targets stay `provisional_historical` unless elasticity evidence supports optimization.
- **Descriptive elasticities are not causal.** The naive estimate is reported to document bias, never used for decisions.
- **Instruments rest on assumptions.** The overidentification test only detects an invalid instrument when the two instruments disagree; exclusion restrictions are documented per instrument in `config/rules.yaml`.
- **Industry-wide cost shocks also move competitor prices.** A customer facing an industry-wide increase has nowhere cheaper to go, so measured responses are flatter than the response to a unilateral move: estimates likely **understate** own-price sensitivity, and overidentification cannot detect it because both instruments shift competitor prices in the same direction. An optional market-price-index control (`market_price_index` reference table) mitigates but cannot eliminate this.
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