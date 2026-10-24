# Margin Engine — Build Spec v2 (CLAUDE.md)

> Claude Code reads this file automatically. Project name `margin-engine` is a placeholder.

---

## 1. Project brief

Build an **auditable pricing and margin decision engine for B2B distributors** (industrial, electrical, HVAC, plumbing, foodservice — any business with negotiated prices).

It ingests ERP invoice exports (plus optional reference tables), reconciles every dollar, and produces:
- An accounting-correct price waterfall: list → invoice → pocket revenue → pocket gross profit → contribution
- Peer-group price dispersion and outlier flags
- Commercial segments and a separate margin-driver analysis
- Historical price reference bands with a true economic margin floor
- Price sensitivity estimates with explicit evidence statuses
- Opportunity sizing at three levels (leakage, gross price opportunity, expected contribution change)
- Cost and tariff pass-through scenarios
- A report where every output ties to a dollar figure or decision, with data coverage and evidence status shown

**Audience:** technical reviewers on GitHub and pricing / finance managers.

**Core principles**
1. **Real-data-ready.** Built and tested on synthetic data. Any real export runs through a mapping config that declares column names **and** business meaning, with no code changes.
2. **Config controls policy, code controls algorithms.** Business policy, data interpretation and model choices live in config. Algorithms live in versioned, tested code. Config is validated by schema.
3. **Accounting first.** Every revenue, deduction and cost reconciles to source totals. No double counting. No dollar silently dropped.
4. **Proven, not claimed.** The generator plants known truth. Tests measure recovery across multiple random seeds.
5. **Fail closed.** When data or assumptions don't support a result, output `insufficient_evidence` or a lower evidence status instead of a confident-looking number.
6. **Honest labels.** Historical benchmarks are not optimal prices. Descriptive elasticities are not causal. Allocated amounts are not actuals.

---

## 2. Run mode and working rules for Claude Code

**Run mode: continuous with checkpoints.** The logic in this spec has been reviewed in advance. Build without waiting for approval between phases, except where a stop condition applies.

- **Run 1 (core engine):** Phases 0–8 continuously → **HARD STOP** for owner review.
- **Run 2 (extended modules):** Phases 9–17 continuously, only after owner types "proceed" → **HARD STOP**.
- **Run 3 (hosted demo UI):** only after owner types "proceed". Phases 18–19 continuously → **HARD STOP for visual review**; after owner approval, Phases 20–23 continuously → **HARD STOP**. See the Run 3 section in 6 for details.

**After every phase**
1. Run the phase's verification plus all quality checks (ruff, mypy, pytest).
2. Append a phase report to `docs/build-log.md`: what was built, verification results vs. targets in section 8 (pass/fail + numbers), decisions made, known gaps.
3. One local git commit: `phase-N: <summary>`.

**Stop conditions (stop, report, wait for the owner)**
- A test or quality check still fails after 3 fix attempts.
- **Any failure in Phase 2 (accounting).** Everything downstream depends on it.
- A section 8 target is missed after 3 attempts **and** a later phase depends on it. Dependencies: Phase 4 → 5; Phase 5 → 7; Phase 6 → 7, 16, 17. If nothing depends on it, log it as a KNOWN GAP and continue.
- The spec doesn't answer a business-logic question. Never guess on business logic.

**Hard rules**
- **Stay in scope.** Log proposed extras in the build log instead of adding them.
- **Never weaken, skip or delete a test** to make it pass. Never loosen a section 8 target silently.
- **Never silently disable a feature** to hit a performance target.
- **Money is exact.** Monetary amounts use Decimal (or integer cents), never floats, for any value that must reconcile.
- **Never push to GitHub.** Commit locally only.
- **Never touch files outside this project folder.**
- **At the start of every run, and after any owner commit, re-read CLAUDE.md from disk. Never work from a cached copy.**
- **Resuming:** if interrupted, read `docs/build-log.md` and continue from the last completed phase.
- **Professional tone** in code, docs and UI. No emojis.

---

## 3. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Package / env | uv |
| Lint + format | ruff |
| Type checking | mypy (strict on `src/`) |
| Tests | pytest, pytest-cov, hypothesis (property-based) |
| CI | GitHub Actions (lint, types, tests) |
| Dataframes | Polars |
| Analytical store | DuckDB |
| Data validation | pandera (Polars backend) |
| Config | YAML + pydantic models (schema-validated) |
| ML / segmentation | scikit-learn |
| Econometrics | pyfixest (fixed effects OLS, IV, Poisson/PPML), linearmodels (IV diagnostics, overidentification tests), statsmodels |
| CLI | Typer |
| Static report | Jinja2 HTML |
| Run 3: web app | React 18 + TypeScript + Vite, static, hosted on GitHub Pages (full stack in the Run 3 section) |

---

## 4. Repo structure (target)

```
margin-engine/
├─ config/
│  ├─ mappings/            # one YAML per source system (columns + semantics)
│  └─ rules.yaml           # policy, thresholds, model choices (sections in 8.10)
├─ src/margin_engine/
│  ├─ ingest/              # load, map, semantic checks
│  ├─ transactions/        # classification, linking returns/credits
│  ├─ costing/             # cost basis, landed cost, vendor rebates
│  ├─ ledger/              # sales-observation view + financial ledger
│  ├─ waterfall/           # staged waterfall, allocations, reconciliation
│  ├─ dispersion/          # peer groups, outliers, ABC
│  ├─ segment/             # commercial segments + margin-driver tree
│  ├─ guidance/            # reference bands, economic floor, statuses
│  ├─ panel/               # customer-item-period panel, zero rows
│  ├─ sensitivity/         # models, diagnostics, evidence status, shrinkage
│  ├─ opportunity/         # three-level sizing, overlap handling
│  ├─ scenarios/           # cost / tariff pass-through
│  ├─ extended/            # Run 2 modules
│  ├─ manifest/            # run manifest, fingerprints
│  ├─ report/
│  └─ cli.py
├─ synth/                  # generator, scenario presets, ground truth
├─ tests/
│  ├─ fixtures/            # hand-calculable accounting cases
│  ├─ property/            # hypothesis tests
│  └─ ...
├─ data/                   # gitignored; sample/ holds small committed demo set
└─ docs/
```

---

## 5. Canonical data model

### 5.1 Mapping semantics
A mapping YAML declares more than column names. For every amount field it declares: business meaning, currency, unit of measure, sign convention, whether discounts are already embedded, cost basis (standard / average / FIFO / landed), effective-date semantics, and reversal handling.

If a semantic needed by a calculation is missing or ambiguous (e.g. a column called `Cost` with no declared basis), **fail with an actionable validation error**. Never guess.

### 5.2 Fields
**Required (invoice lines)**
`source_line_key, invoice_id, invoice_date, customer_id, item_id, quantity, uom, unit_price or net_amount, unit_cost (with declared cost_basis)`, plus `transaction_type` or a configured rule to derive it.

**Optional (each unlocks features; report what was skipped and why)**
`order_id, list_price, on_invoice_discount, customer_rebate_amount, rebate_program_id, freight_billed, freight_concession, freight_expense, vendor_rebate, inbound_freight, duty, credit_reason, original_invoice_ref, restocking_fee, return_freight_billed, sales_rep_id, channel, ship_to_id, bill_to_id, parent_account_id, contract_id, currency, item_category, item_family, supplier_id, weight, cube, handling_units, kit_parent_id, item_role`

**Optional reference tables**
Price list history (effective-dated), UOM conversions, FX rates, rebate program definitions, contracts and price-protection periods, quotes with outcomes, substitution groups, inventory receipts, cost-to-serve activity rates, return freight expense log.

### 5.3 Transaction classification
| Case | Treatment |
|---|---|
| Ordinary sale | Included in sales observations and the financial ledger |
| Return | Excluded from sales observations; financial effect kept in the ledger and linked to the original sale when possible. Classified by return reason (customer_error / distributor_error / defective, mapped from the source reason code). Restocking fees and return freight billed to the customer are recovery revenue; return handling and return freight expense are cost-to-serve. Actual amounts from the export when mapped, otherwise policy/config estimates labeled `estimated` |
| Credit memo | Classified by reason. Price corrections and customer rebates reduce realized revenue; non-price claims go to the appropriate cost category |
| Exact duplicate | Deduplicated only on a stable source key or verified duplicate identity; uncertain identical rows are flagged, not removed |
| Missing cost | Revenue and price kept; excluded from cost-dependent metrics; missing-cost revenue share reported |
| Zero cost | Flagged for review, not treated as missing (genuine zero-cost and non-inventory items exist) |
| Negative price or quantity | Classified by transaction type, never treated as an ordinary negative-price sale |
| Missing list price | Available waterfall stages calculated and labeled incomplete; list price never manufactured from invoice price |
| Zero quantity | Excluded from unit-price math; investigated as fee, adjustment or malformed record |
| Zero or negative net revenue | Dollars kept; no margin % reported when the denominator is ≤ 0 |
| Reissued invoices, split shipments | Not treated as duplicates without verified identity |

### 5.4 Two views
1. **Sales observations:** clean sale lines for estimating purchase behavior.
2. **Financial ledger:** sales, returns, credits, rebates and adjustments, reconciled to source totals.

Excluding returns and credits from the observations view **never** removes their economic effect from the ledger.

### 5.5 Waterfall (separate revenue and cost ledgers)
1. **Gross list value** = effective-dated list price × normalized quantity
2. **Invoice revenue** = after on-invoice discounts and applicable charges
3. **Pocket revenue** = after customer rebates, freight concessions and attributable credits
4. **Pocket gross profit** = pocket revenue − matched product cost
5. **Contribution** = pocket gross profit − attributable cost-to-serve (when available)

```text
Pocket gross margin = (Pocket revenue − Product cost) / Pocket revenue
Contribution        = Pocket revenue − Product cost − Cost to serve
```

**Treatment rules**
- Customer freight concessions reduce pocket revenue. Actual outbound freight expense goes to cost-to-serve, not the price waterfall.
- Vendor rebates and incentives adjust acquisition cost, not the customer-facing waterfall.
- Product cost must state whether it includes inbound freight, duty and vendor rebates, so nothing is counted twice or lost.

**Allocations**
- Basis priority: actual line amount → weight/cube → handling units → lines/orders → extended-price share (fallback only).
- Every allocated amount records method, source amount, driver and status: `actual`, `allocated` or `estimated`.
- Rounding residuals are distributed deterministically so lines sum exactly to the source amount.
- When allocation isn't possible, keep a visible residual. Never force costs onto SKUs to make a report look complete.

**Reported measures:** invoice gross profit and margin, pocket gross profit and margin, contribution (when cost-to-serve exists), and the share of revenue and lines excluded from each metric.

---

## 6. Phases

### RUN 1 — Core engine

#### Phase 0 — Scaffold and tooling
- uv project, ruff, mypy, pytest, hypothesis, pre-commit, GitHub Actions.
- Repo structure from section 4. Pydantic config models for `rules.yaml` sections (8.10).
- **Verify:** `uv sync`, `ruff check`, `mypy`, `pytest` pass; CI config valid.

#### Phase 1 — Synthetic generator with planted truth
Seeded, parameterized generator producing a fake ERP export (column names deliberately differ from canonical), reference tables, and `ground_truth.json`.
- **Entities:** customers (industry, region, size, channel, contract type), items (family, category, supplier, weight, cube), reps, 24 months.
- **Commercial structure:** effective-dated list price history, contracts, customer rebates, on-invoice discounts, freight concessions vs. freight expense, vendor rebates, landed cost components.
- **Planted truth:** commercial segment structure, true own-price elasticity per segment, base price index per segment, all rebate and freight amounts.
- **Active assortment and availability:** which customer-item pairs can buy in each month, and stockouts — so true zero-purchase periods exist.
- **Endogeneity:** time-varying unobserved demand shocks; reps discount when demand is weak or the customer is price-sensitive. The expected direction of naive bias is recorded in ground truth.
- **Two instruments:** supplier-specific cost shocks and category tariff shocks, exogenous to demand by default.
- **Transactions:** returns linked to originals, credit memos with reasons, zero-cost service items, missing costs, UOM mismatches, verified and look-alike duplicates, outliers, sparse long-tail SKUs.
- **Scenario presets:** `well_identified`, `weak_instrument`, `invalid_instrument` (one instrument correlated with demand shocks), `sparse`, `price_constant`.
- Scale option to 500k+ lines.
- **Verify:** same seed gives identical output; distributions match parameters; every mess type appears at its configured rate; each preset produces its intended condition.

#### Phase 2 — Ingest, classification, ledger, waterfall, reconciliation
- Mapping with semantics (5.1); actionable errors for missing semantics.
- Transaction classification (5.3); two views (5.4); staged waterfall and allocations (5.5).
- **Deterministic fixtures:** hand-calculable cases for a normal sale, stacked discounts, customer rebate, vendor rebate, freight concession, freight expense, partial return, credit memo, zero-cost service, missing-cost item. Assert every waterfall stage to the cent.
- **Property tests (hypothesis):** varied quantities, prices, sign conventions and deductions always reconcile to source totals.
- **Mapping tests:** different column names, reordered columns, missing optional fields, ambiguous semantics, alternative date formats, malformed input. Equivalent canonical data from different mappings must give identical outputs.
- Store both views in DuckDB with an audit trail back to source lines.
- **Verify:** all fixtures exact; reconciliation exact after rounding; planted rebate and freight recovery per section 8.

#### Phase 3 — Descriptive analytics
- Normalize to a comparable unit and price index (price ÷ effective-dated list, or price ÷ item median when list is missing; method labeled).
- Peer-group dispersion: item (or family) × UOM × contract regime × period, with minimum cohort size. Report median, IQR, P10/P50/P90, discount-to-list distribution.
- Outliers: modified robust z-score (0.6745 × deviation / MAD) at 3.5, within peer groups. If MAD = 0, fall back to the configured rule and label the method used.
- Two separate flags: `data_quality_anomaly` and `commercial_pricing_outlier`. Original records are preserved.
- ABC: **revenue ABC**, plus separate unit ABC and order-frequency ABC.
- **Verify:** planted outliers detected per section 8; MAD-zero fallback tested; flag types never mixed.

#### Phase 4 — Segmentation (two separate outputs)
- **Commercial segments:** decision tree on the normalized price index using **context features only** (industry, size band, region, channel, contract type, item family, item role, order-size band, revenue ABC). Realized price, discount, cost and rebate columns are excluded to prevent leakage. Output readable segment rules.
- **Margin-driver tree:** predicts pocket margin % and may use discount and cost features. Outputs are labeled *explanatory drivers*, never segments that justify price increases.
- Minimum leaf sizes per section 8, including distinct-customer and distinct-item minimums.
- Stability: bootstrap resamples compared with partition agreement (ARI), not raw leaf IDs. Also report how many price recommendations change across resamples.
- **Verify:** out-of-sample ARI vs. planted segments and bootstrap stability per section 8 on `well_identified`.

#### Phase 5 — Price guidance
- **Historical reference bands** (P25 / P50 / P75) on the normalized price index within commercial segments. Observation unit is configurable; default is one representative price per customer × item × month.
- **Economic floor** on pocket margin. With minimum margin `m`, cost `C`, percentage post-invoice deductions `d` and fixed per-unit deductions `F`:
  ```text
  Pocket floor  = C / (1 − m)
  Invoice floor = (C / (1 − m) + F) / (1 − d)
  ```
  Floors are computed at full precision and rounded once at the end; default rounding is ceiling (policy: a floor exists to guarantee the margin), with half-even as a config option. d includes the customer's rebate rate and realized freight-concession rate.
- Target and start are **provisional** (labeled `provisional_historical`) until Phase 7 can support optimization.
- Line statuses: `in_band`, `below_reference`, `below_floor`, `above_reference`, `insufficient_pricing_headroom` (corridor infeasible against the floor), `insufficient_data`.
- **Verify:** floor math fixtures (e.g. cost $100, m = 5% → $105.27 pocket floor at ceiling rounding); infeasible corridors flagged, never relabeled.

#### Phase 6 — Price sensitivity
- **Panel:** customer × item × month, with zero-quantity rows only where the pair is in active assortment and the item is available (rule in section 8).
- **Models:** descriptive log-log on positive quantities; fixed-effects specification where within-pair price variation exists; Poisson PPML where zeros are meaningful; IV with the configured instruments.
- **Diagnostics:** first-stage strength, identifying-variation coverage, weak-IV-robust confidence intervals (Anderson-Rubin where applicable), overidentification test when two instruments exist, robustness across alternative specifications, standard errors clustered at the level where instruments vary.
- **Evidence statuses:** `insufficient_data`, `descriptive_elasticity`, `iv_estimated_elasticity`, `causal_elasticity_supported`. The top status requires all criteria in section 8 to pass, plus a documented exclusion rationale per instrument in config.
- **Partial pooling:** empirical Bayes shrinkage toward the parent segment. Report raw estimate, shrunk estimate, effective sample size and uncertainty.
- Scope: own-price elasticity only in Run 1.
- **Verify:** all section 8 sensitivity targets across seeds; naive bias in the planted direction; `weak_instrument` and `invalid_instrument` presets never reach `causal_elasticity_supported`.

#### Phase 7 — Opportunity and cost scenarios
- **Three levels, never summed across levels:**
  - Observed leakage: reconciled dollars lost to discounts, rebates and concessions.
  - Gross price opportunity: eligible quantity × positive gap to proposed pocket price, at unchanged volume.
  - Expected contribution change: `(P1 − C1)·Q(P1) − (P0 − C0)·Q(P0)`, only where elasticity status is `causal_elasticity_supported`; `iv_estimated_elasticity` shown as a labeled sensitivity range; otherwise fixed-volume only.
- **Overlap handling:** combined scenarios are modeled jointly with eligibility rules; overlapping opportunities are never double counted.
- **Target optimization:** `P* = argmax (P − C_variable)·Q(P)` subject to floor and constraints, only where elasticity is causal-supported; otherwise target stays provisional.
- **Cost / tariff scenarios:** incremental landed cost by item, supplier or category, with effective dates; pass-through strategies (none, partial %, full, segment-weighted); contract-protected lines excluded from pass-through. Output incremental cost, recovered amount and %, residual exposure, and expected contribution effect only when supported.
- Every opportunity record carries baseline period, action, constraints, quantity basis, price assumptions, evidence status and calculation trace.
- **Verify:** scenario math fixtures; rollups reconcile; overlap test proves no double counting.

#### Phase 8 — CLI, report, manifest, docs
- `margin-engine run --data <file> --mapping <yaml>` runs the pipeline.
- **Run manifest:** config hash, mapping, input file fingerprint, software versions, seed, cleaning decisions, parameters — so any result can be reproduced months later.
- Outputs: Parquet/CSV (Power BI and Excel readable) and an HTML report with data coverage indicators and evidence statuses on every model output.
- README: problem, method, **validation results table (planted vs. recovered, across seeds)**, how to run on real data, limitations, roadmap. `docs/methodology.md` for model detail.
- **Performance:** 500k-line full pipeline with stage timings, hardware and memory reported.
- **Verify:** end-to-end run completes; report renders; README instructions work from a clean clone.
- **HARD STOP.**

### RUN 2 — Extended modules
Each phase extends the generator with planted truth for its feature, then implements and validates it. Each module is optional at run time: if its input data is absent, it switches off and the report says so.

#### Phase 9 — Multi-currency and units of measure
- FX table and reporting currency; documented conversion policy (default: rate as of invoice date).
- UOM conversion table; all price comparisons in a canonical selling unit.
- **Verify:** converted totals reconcile; mixed-currency and mixed-UOM fixtures exact.

#### Phase 10 — Customer hierarchies and contracts
- Ship-to → bill-to → parent account. Analysis level configurable.
- Contract prices and price-protection periods; protected lines flagged in guidance and excluded from pass-through.
- **Verify:** national-agreement vs. local-branch behavior separated as planted; protected lines never receive price-increase recommendations.

#### Phase 11 — Tiered and retrospective rebates
- Program definitions: thresholds, tiers, measurement period, settlement date.
- Accrued estimates during the period (`estimated`), trued up at settlement (`actual`). Waterfall shows both and labels which is used.
- **Verify:** planted tier outcomes recovered; accrual vs. settlement reconciles.

#### Phase 12 — Kits, bundles and free goods
- Kit parent and component lines; revenue allocated to components by configurable method (default: relative standalone price).
- Free goods classified as their own type, never flagged as price outliers.
- **Verify:** kit allocation sums exactly to kit revenue; free goods excluded from dispersion.

#### Phase 13 — Cost-to-serve and order economics
- Activity rates (per order, line, delivery, return, special handling) from config.
- Freight and handling allocation by weight, cube or handling units where available.
- Order-level and customer-level contribution. Flags service-policy opportunities (minimum order, delivery frequency) separately from pricing opportunities.
- **Verify:** planted unprofitable small-order customers identified; allocations reconcile.

#### Phase 14 — Inventory costing timing
- Receipts table; average, FIFO and standard cost methods.
- Tariff scenarios state whether they model new receipts, the existing pool or a blend, and when the cost change reaches COGS.
- **Verify:** timing fixtures exact for each method.

#### Phase 15 — Product roles and key value items
- Configurable roles: key value item, strategic contract, specialty, long tail.
- Data-driven KVI candidate score (purchase frequency, customer breadth, price comparability proxy), labeled as candidates.
- Role-specific guidance policy (e.g. tighter corridors on KVIs).
- **Verify:** planted KVIs ranked in top candidates per section 8.

#### Phase 16 — Quotes and win/loss
- Optional quotes table (quote date, customer, item, quantity, quoted price, outcome).
- Generator simulates quotes with outcomes from a planted win-probability curve.
- Win-probability model by segment (logistic on price index), with uncertainty and evidence status.
- Where supported, target optimization uses `(P − C)·Q(P)·P_win(P)`; start price informed by the planted negotiation gap between quote and close.
- **Verify:** planted win-curve price coefficient recovered per section 8.

#### Phase 17 — Substitution and cross-price effects
- Substitution groups from config or input.
- Generator plants cross-price elasticities within groups.
- Cross-price estimates only within groups and only where identified; same evidence statuses as Phase 6.
- Opportunity scenarios account for volume shifting to substitutes.
- **Verify:** planted cross-price direction recovered on `well_identified`; unsupported cases labeled, not estimated.
- **HARD STOP.**

### RUN 3 — Hosted demo UI

**Goal:** a static, hosted web app (GitHub Pages) that presents the engine's results on the committed sample data. Anyone with the link can explore it: no install, no backend, no API keys. It must look polished and intentional; it is the first thing LinkedIn viewers see.

**Architecture**
- The Python engine exports a versioned JSON data bundle; the React app reads only that bundle. No server, no runtime computation of business numbers in the browser.
- Every number shown must come from the engine's exports. The UI formats and filters; it never recalculates financials.
- Interactive "what-if" features use precomputed grids from the engine (e.g. pass-through at 0–100% in 10% steps), so the browser never re-implements engine logic.

**Run mode:** Phases 18–19 continuously → **HARD STOP for visual review** → Phases 20–23 continuously → **HARD STOP**. After every Run 3 phase, run the Python checks (ruff, mypy, pytest) **and** the web checks (TypeScript build, Vitest, Playwright where present).

**Stack**
| Layer | Choice |
|---|---|
| App | React 18 + TypeScript (strict) + Vite |
| Styling | Tailwind CSS with project design tokens (CSS variables) |
| Components | Radix UI primitives (accessible dialogs, tabs, tooltips, popovers) |
| Charts | Recharts, with custom-styled waterfall, band, and interval charts |
| Tables | TanStack Table (sorting, filtering, pagination; virtualized where large) |
| Routing | React Router, hash routing (GitHub Pages compatible) |
| Types | TypeScript types generated from the bundle's JSON Schema |
| Tests | Vitest (units), Playwright (end-to-end, visual snapshots), axe-core (accessibility) |
| Deploy | GitHub Actions → GitHub Pages on push to `main` |

#### Phase 18 — Web data bundle
- New CLI command `margin-engine export-web --data <dir> --mapping <yaml> --out web/public/data` producing a versioned JSON bundle: summary KPIs, reconciliation and residuals, waterfall (overall, by segment, by customer), dispersion and outliers (top N), segments and rules, guidance (all pairs), sensitivity with diagnostics and evidence status, opportunities (three levels), order and customer economics, scenario grids, optional modules (win curves, cross-price, inventory costing), validation results, and run manifest.
- Aggregated, not raw: no full ledger in the bundle. Target total size under 5 MB.
- Money as integer cents in the bundle; the UI formats.
- Publish a JSON Schema for the bundle; generate TypeScript types from it.
- **Explanations:** generate plain-English "why" text for each guidance recommendation and opportunity at export time from computed values, using deterministic templates (no LLM, no API key). Every number in an explanation comes from the outputs.
- **Verify:** schema validation passes; parity tests prove every bundle figure equals the corresponding engine export (cents-exact); deterministic (byte-identical across runs); size target met.

#### Phase 19 — Design system, app shell, Overview page
1. **Design plan first.** Write `docs/ui/design-plan.md`: subject, audience, primary job, 4–6 named palette colors (hex), typefaces and roles, type scale, spacing scale, layout concept with ASCII wireframes, chart style rules, and the evidence-status visual language (below). Review it against the brief and the anti-patterns list; revise any part that reads as a generic default, and record what changed and why.
2. **Implement tokens** as CSS variables with light and dark themes. Tabular (monospaced-width) numerals for all figures.
3. **App shell:** top navigation, persistent "Synthetic demo data" notice, link to the GitHub repo, responsive layout down to 375 px wide.
4. **Overview page** built to final quality: headline outcome, reconciliation status, the price waterfall as the signature visual, three to five key findings each linking to its detail page, data coverage summary.
5. Save desktop and mobile screenshots of the Overview in light and dark mode to `docs/ui/review/`.
- **Verify:** Playwright loads the page with no console errors; axe reports no serious or critical violations; screenshots saved.
- **HARD STOP for owner visual review.** Do not build further pages until the owner approves the design direction.

#### Phase 20 — Detail pages
| Page | Must show |
|---|---|
| Price waterfall | Interactive waterfall; switch overall / segment / customer; leakage by component in dollars and % |
| Customers & orders | Sortable, filterable customer table (contribution, returns impact, % revenue excluded, contribution after activity cost); customer drill-down with its waterfall, top items, guidance statuses, service flags |
| Segments | Readable segment rules, size, price-index distribution per segment |
| Price guidance | Customer × item lookup; band chart with floor, reference band and current price; status badge; plain-English explanation |
| Price sensitivity | Per-segment elasticity with confidence intervals and evidence status; expandable diagnostics (first stage, overidentification, robustness); naive vs. corrected comparison |
| Opportunity | The three levels side by side, visually separated and never summed; top opportunities with explanation and evidence status |
| Scenarios | Cost/tariff pass-through using the precomputed grid (slider snaps to grid points); margin, volume, recovered and residual exposure |
| Data & methodology | Reconciliation table, residual explanations, coverage, captured warnings, validation results table, link to `methodology.md` |
| Optional modules | Win curves, cross-price, inventory costing, each shown only when present in the bundle, with stated reason when absent |

- Empty and edge states designed (no data, insufficient evidence, module absent): each explains what happened in plain language.
- **Verify:** every route loads without console errors; spot-check parity (Playwright reads displayed values on each page and compares to the bundle).

#### Phase 21 — Interaction and polish
- Global customer / segment filters where meaningful; URL reflects state (shareable links).
- Keyboard navigation and visible focus everywhere; reduced-motion respected.
- One deliberate page-load moment (e.g. the waterfall building on the Overview); otherwise motion only in response to user actions.
- Copy pass: sentence case, plain language, labels named for what users understand, consistent action names.
- **Verify:** Playwright interaction tests for filters, drill-downs, lookup and slider.

#### Phase 22 — Quality gates
- Playwright end-to-end suite across all pages at desktop (1440 px) and mobile (375 px) widths.
- Visual snapshot tests for every page in light and dark mode.
- axe: no serious or critical violations on any page.
- Lighthouse (desktop): performance ≥ 90, accessibility ≥ 95, best practices ≥ 95.
- Production bundle size reported; initial JavaScript under 500 KB gzipped.
- **Verify:** all gates pass in CI.

#### Phase 23 — Deploy
- GitHub Actions workflow: on push to `main`, run engine export on the committed sample, build the app, run Playwright, deploy to GitHub Pages.
- README gains the live demo link and a screenshot of the Overview.
- **Verify:** workflow file valid and documented; local production build served and smoke-tested.
- **HARD STOP.** The owner enables GitHub Pages in repository settings and pushes.

#### Evidence-status visual language (applies everywhere)
Status must never be conveyed by color alone. Each status has a distinct fill treatment, label, and tooltip explaining what it means and why it was assigned:

| Status | Treatment |
|---|---|
| `causal_elasticity_supported` / `estimated_win_curve` | Solid fill, full-strength color |
| `iv_estimated_elasticity` | Partial / hatched fill |
| `descriptive_elasticity` | Outline only |
| `insufficient_data` / `insufficient_precision` | Muted, dashed outline, with reason |
| `allocated` / `estimated` amounts | Small marker plus tooltip naming the method |

#### Design anti-patterns to avoid
Generic card grids with identical radius and shadow on everything; gradient washes as decoration; all-caps eyebrow labels above every heading; meta strings joined with middle dots; monospace for small labels; arrows appended to button text; numbered markers on content that isn't a sequence; fade-in animations on every section; cream background with terracotta accent; near-black with a single neon accent.

#### Run 3 out of scope
- Uploading user data in the hosted demo (static site; documented as a local-CLI feature)
- Server, database, authentication
- LLM-generated text at runtime

---

## 7. Out of scope
- Real-time ERP integration
- Multi-user auth, server hosting, billing (the static GitHub Pages demo in Run 3 is the one hosted piece)
- Claims of real-world accuracy beyond what tests demonstrate

---

## 8. Logic defaults and validation targets

**These are starting defaults, all configurable.** Targets apply to synthetic data at default generator settings. If a target proves unrealistic, report the actual result and reasoning; never loosen it silently.

### 8.1 Data handling
As in section 5.3. Duplicates removed only on verified identity.

### 8.2 Money and rounding
| Item | Default |
|---|---|
| Monetary type | Decimal |
| Rounding | Half-even to reporting currency precision (cents) |
| Allocation method | Largest-remainder (Hamilton): floor each share to the cent, distribute remaining cents to the largest fractional remainders; ties broken by larger absolute weight, then source_line_key; signed weights supported |

### 8.3 Outliers and dispersion
| Item | Default |
|---|---|
| Method | Modified robust z-score, threshold 3.5 |
| Peer group | Item × UOM × contract regime × quarter; fall back to item family if fewer than 30 observations |
| MAD = 0 fallback | Flag prices outside the most common price by more than 10%; method labeled |

### 8.4 Segmentation
| Item | Commercial segments | Margin-driver tree |
|---|---|---|
| Target | Normalized price index | Pocket margin % |
| Features | Context only (no price, discount, cost, rebate) | Context + discount and cost features |
| Min leaf | Larger of 500 lines or 0.5% of lines, plus ≥ 20 customers and ≥ 10 items | Same |
| Max depth | 5 | 4 |

ABC splits (revenue, unit and frequency versions): A = top 80%, B = next 15%, C = last 5%.

### 8.5 Price guidance
| Item | Default |
|---|---|
| Reference bands | P25 / P50 / P75 of normalized price index within commercial segment |
| Observation unit | Median price per customer × item × month |
| Minimum margin | 5% pocket gross margin, floor per Phase 5 formula |
| Floor rounding | Floors computed at full precision, rounded once at the end. `ceiling` (default: a floor must guarantee the margin) or `half_even` |
| Target / start | Provisional (P50 / P75, never below floor) until optimization is supported |

### 8.6 Sensitivity
| Item | Default |
|---|---|
| Panel period | Month |
| Active assortment (zero rows) | Pair purchased in the prior 6 months and item available |
| First-stage strength | Effective F ≥ 10 (rule-of-thumb threshold, configurable) |
| Clustering | Level where the instrument varies (supplier or category × month) |
| Shrinkage | Toward parent commercial segment |
| Win-curve gating | Segments with >= `min_quotes_per_segment` (default 400) quotes and relative SE <= `win_curve_max_rel_se` (default 0.10) carry `estimated_win_curve` and gate at ±15% relative per segment; thinner segments are `insufficient_data`, imprecise fits `insufficient_precision` (CI reported, not gated) |
| `causal_elasticity_supported` requires | First-stage threshold met; overidentification test not rejected (when 2 instruments); robustness spread within 0.20 across specifications; 95% CI width under 1.0; exclusion rationale documented in config |

### 8.7 Opportunity and scenarios
| Item | Default |
|---|---|
| Baseline window | Last 12 months |
| Eligibility | Excludes contract-protected lines and lines with `insufficient_data` |
| Pass-through scenarios | 0%, 50%, 100% shown side by side; full pass-through never assumed |

### 8.8 Validation targets
| Check | Target |
|---|---|
| Accounting fixtures | Exact to the cent |
| Ledger reconciliation | Exact to source totals after rounding |
| Rebate / freight recovery — actual amounts | Exact |
| Rebate / freight recovery — allocated amounts | Within ±5% of planted |
| Planted outlier detection | ≥ 90% recall, ≤ 5% false-flag rate |
| Commercial segments vs. planted (out-of-sample) | ARI ≥ 0.70 on `well_identified` |
| Segment stability (bootstrap) | Median ARI ≥ 0.80 |
| Elasticity, `well_identified`, 5 seeds | Median absolute error ≤ 0.10; P90 ≤ 0.15; worst case reported |
| CI coverage, 20 simulations | 95% intervals cover truth 90–98% of the time |
| Naive estimator | Biased in the planted direction on every seed |
| `weak_instrument` | Never `causal_elasticity_supported`; weak-IV-robust CI reported |
| `invalid_instrument` | Overidentification test rejects on ≥ 80% of seeds; status downgraded |
| `price_constant` / `sparse` | `insufficient_data` or `descriptive_elasticity`, never higher |
| KVI candidates (Phase 15) | ≥ 80% of planted KVIs in top-ranked candidates |
| Win-curve price coefficient (Phase 16) | Within ±15% relative of planted |
| Performance | Run 1 full pipeline, 500k lines, under 2 minutes; stage timings and hardware reported |

### 8.9 Known limitation to document
The overidentification test only detects an invalid instrument when the two instruments disagree. Exclusion restrictions remain economic assumptions that no test fully proves; the README must say so.

### 8.10 Config sections (`rules.yaml`)
`schema, mapping, units_and_currency, transactions, returns, hierarchy, cost_to_serve, costing, waterfall, outliers, segmentation, price_guidance, sensitivity, opportunity, tariff_scenarios, reporting`

---

# RUN 4 — Correct, real-data-ready, and live

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

---

# RUN 5 — Scale, compliance, and the Action center

**Goal:** make the engine handle real ERP volumes, add pricing-law guardrails, and turn findings into decisions a team can act on and track: an action list with owners and status, approvals, an ERP-ready price export, and results tracking that compares what happened against what the engine predicted.

## 0. Run mode and rules

- **Branch:** all work on a new branch `run5` from `main`. Never commit to `main`, never push. The owner pushes and opens a PR; the PR runs CI and the Linux browser gates.
- **Continuous run** through every phase in order. Stop only on a stop condition.
- **After every phase:** run the applicable Python checks (ruff, ruff format, mypy src api, full pytest) and web checks (tsc, vitest, build, Playwright); confirm each exit code from the process itself; append a phase report to `docs/build-log.md`; one commit `phase-N: <summary>`.
- This machine has 14 GB RAM. Run heavy jobs (full test suite, validation script, scale ladder, scenario matrix full tier) as background processes writing to log files; read results from the logs. Keep chat output short.
- **Verify like CI before the final commit:** fresh clone of `run5` into a temp folder, `uv sync --frozen --all-groups`, full pytest; and the Pages workflow steps (`margin-engine export-web --demo`, `npm ci`, build, `CI=true SKIP_SNAPSHOTS=1 npx playwright test`). Never rely on files that exist only on this machine.
- **Stop conditions:** a check still fails after 3 fix attempts; an existing validation target fails (report actuals, never loosen); the spec doesn't answer a business-logic question.
- **Determinism and parity:** every existing export must stay byte-identical across runs, and every UI figure must still trace to engine output. New features get the same treatment.
- **Language:** everything a viewer reads is plain English through the label dictionary: no raw codes, variable names, abbreviations ("pcts", "P20"), or underscores in text, tooltips, titles, or aria-labels.

## Phase 35 — Scale refactor

Measured problem: about 12 KB of RAM per invoice line (2.92 GB peak at 241k lines); no lazy or streaming processing; 85 ledger columns including 32 duplicate dollar columns; 62 row-wise Python conversions.

- Money stays integer cents inside the engine; `_usd` columns are derived only when writing CSV exports. Drop intermediate columns as soon as a stage finishes.
- Categorical/enum dtypes for IDs and codes.
- Polars lazy frames with the streaming engine and `sink_parquet` for ingest, classification, waterfall, and dispersion.
- Month-partitioned processing for allocations and the waterfall (settlements by period).
- DuckDB for the heaviest joins and aggregations (spills to disk under memory pressure).
- Replace row-wise conversions (`to_dicts`, `iter_rows`, `map_elements`) on line-level paths with columnar expressions.
- Real-data validation behind a `--validate` flag and a separate `margin-engine validate` command.
- Cache stage outputs keyed by input fingerprint; replay ticks reprocess only new data.
- **Verify:** all exports byte-identical before and after (determinism and parity tests). Re-run the memory-aware scale ladder; record bytes per line, peak memory and per-stage runtime before and after in validation.json.

## Phase 36 — Fiscal calendars

Configurable fiscal calendar: calendar months (default), 4-4-5, 4-5-4, 5-4-4, or a custom period table. Used for periods, baseline windows, the books tie-out, and every period label in the UI. Fixtures for each calendar type, including year boundaries and 53-week years.

## Phase 37 — Pricing compliance

- Robinson-Patman review flag: customers who are likely competitors (configurable definition, default same industry and region) receiving recommended prices for the same item that differ by more than a configurable threshold get flagged. Show the cost-to-serve difference between them beside the flag as supporting documentation.
- Public-data-only rule: the market price index control accepts public sources only; document why (antitrust concerns about competitors sharing non-public prices through algorithms).
- Methodology section "Pricing-law considerations," written as orientation, not legal advice.
- **Tests:** flag triggers at the threshold boundary; no flag for non-competing customers; cost-to-serve evidence matches the ledger.

## Action center (Phases 38–42)

The engine currently shows findings. These phases turn findings into actions: proposed, reviewed, approved, exported to the ERP, implemented, and then measured.

## Phase 38 — Action model and generation

- **Action types,** generated from engine outputs, each with a plain-English title, rationale, evidence status, and expected dollar impact (using the right opportunity level, never summed across levels):
  - Price change: customer × item to a proposed price (from guidance and opportunity).
  - Below-floor fix: pairs priced below the binding floor.
  - Cost pass-through: from a scenario at a chosen pass-through level.
  - Service policy: minimum order or delivery-frequency changes for money-losing customers.
  - Rebate/discount review: customers whose leakage is far above their segment.
- **Lifecycle:** proposed → under review → approved or rejected → exported → implemented → measured. Each transition is an event in an append-only event log (who, when, from-status, to-status, note). Current status is derived from the log, never edited in place.
- **Fields:** owner, due date, priority (configurable rules, default by expected impact and evidence), compliance flags, and links to the evidence (guidance band, explanation, segment, elasticity status).
- **Guardrails:**
  - No action may propose a price below the binding floor.
  - Expected volume response only where evidence is causal-supported and inside price support; otherwise fixed-volume, labeled.
  - Actions on contract-protected lines are blocked, with the reason shown.
  - Actions with a Robinson-Patman flag require a written justification at approval.
- **Storage:** DuckDB in API mode. The static demo ships a read-only generated action set with a simulated history.
- **Tests:** generation is deterministic; every action traces to its source records; guardrails enforced; status always equals the replayed event log.

## Phase 39 — Review and approval (maker-checker)

- Roles: analyst (proposes, edits), approver (approves or rejects), viewer. In local mode, identity is a configured user name, documented clearly as not authentication (real login belongs to a future hosting phase).
- An approver can't approve their own proposal (separation of duties).
- Approval records: approver, timestamp, scope, reason; justification required when a compliance flag is present.
- Bulk approve with per-item guardrail checks; any failing item is rejected with the reason.
- API endpoints for every transition (FastAPI, OpenAPI updated); idempotent; rate-limited like the rest.
- **Tests:** self-approval blocked; justification enforced; event log complete; API contract tests.

## Phase 40 — ERP-ready price export

- Export approved price actions as a price file: generic CSV by default, plus a configurable column template (header names, date format, decimal separator, units) so it matches what a given ERP imports.
- Each row carries effective date, previous price, new price, UOM, approval ID, and action ID.
- A diff summary (count of changes, total expected impact, largest changes) shown before export and written beside the file.
- Exporting moves actions to "exported" and logs it. Re-exporting is idempotent.
- **Tests:** only approved actions export; template variants; round-trip (re-import the file through the engine's own ingest and confirm prices match).

## Phase 41 — Results tracking

The feedback loop, and the engine's route to real-world validation.

- After an action is marked implemented, measure over a configurable window (default 90 days):
  - Price realization: did the invoiced price actually move to the new price, or did it erode back?
  - Volume outcome vs. prediction: actual volume against the engine's predicted volume, with a comparison group of similar untreated pairs (same segment, no change) to separate the price effect from general trends (a difference-in-differences comparison).
  - Margin outcome: pocket margin and contribution before vs. after.
- Model calibration report: prediction error by evidence status (causal-supported vs. IV-estimated vs. fixed-volume), feeding a "real-world accuracy" table in validation.json and the Methodology page once enough actions are measured. Below a configurable minimum count, show "not enough implemented actions yet" instead of a number.
- Synthetic validation of the tracker: the generator gains an option to simulate implemented price changes on chosen pairs, with planted true responses afterward. The tracker must recover the planted effect within stated error on the well-identified world, and must report "insufficient evidence" on worlds where it can't. Record in validation.json.
- **Tests:** realization math fixtures; the comparison-group method on planted data; minimum-count gating.

## Phase 42 — Action center UI

- New "Action center" page:
  - Queue view: filters for type, status, owner, priority, evidence, compliance flag, and expected impact range (using the shared filter system), with sortable columns and virtualization.
  - Action detail: plain-English rationale, evidence links (band chart, explanation, segment), compliance flags with explanation, the full event history, and outcome charts once measured.
  - In API mode: propose, edit, approve, reject, bulk-approve, export, and mark implemented, with loading and error states, optimistic updates rolled back on failure, and clear role-based disabled states.
  - In the static demo: read-only, with the generated history, labeled as demo data.
- Overview: an "Open actions" summary (count by status, expected impact by evidence level, latest results).
- Results view: realized vs. predicted outcomes and the calibration table.
- **Tests:** Playwright for queue filters, detail, every lifecycle transition in API mode, role restrictions, the self-approval block, export, and the read-only demo; parity of impact totals with the engine; axe and the raw-code scanner on the new pages.

## Phase 43 — Drift monitoring (live mode)

Each refresh compares against the previous run: data-quality rates, segment assignments (adjusted Rand index), elasticity estimates and statuses, and status counts. Changes beyond configurable thresholds are flagged in the API and UI with a plain-English explanation ("Segment 2's price sensitivity moved from −1.4 to −0.9 since last week"). Tests with planted shifts.

## Phase 44 — Supply-chain security

Dependabot configuration for Python, npm, and GitHub Actions. CI steps for pip-audit and `npm audit --audit-level=high`; document how to handle a finding.

## Phase 45 — Container

Dockerfile and compose file for API + UI + scheduler. CI builds the image and runs a smoke test (GitHub-hosted runners have Docker even though this machine doesn't). README "Run with Docker" section.

## Phase 46 — Display polish and rendering tests

- Display names for data values: categorical values from data render with proper capitalization and an acronym list ("OEM", not "oem"; "Construction", not "construction"), configurable in the mapping; raw values stay in exports.
- Playwright test asserting each guidance, sensitivity, opportunity, and action status renders with its label and plain-English explanation.

## Phase 47 — Documentation and report

- README: Action center (workflow, roles, export, results tracking), fiscal calendars, compliance flag, Docker, scale results; validation table regenerated from validation.json.
- Methodology: action generation rules, guardrails, the results-tracking method and its limits, pricing-law considerations.
- `docs/run5-report.md`: every phase, what changed, tests added, before/after numbers (memory and runtime per line, action counts, tracker recovery), known gaps, and decisions waiting on the owner.
- Final commit on `run5`, then **stop**. Do not merge, do not push.
