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
- **Run 3 (API, UI, agent):** Phases 18–20, only after owner types "proceed".

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
| Run 3: API | FastAPI |
| Run 3: UI | React + TypeScript + Vite |
| Run 3, optional | Claude API (explanations only) |

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
`order_id, list_price, on_invoice_discount, customer_rebate_amount, rebate_program_id, freight_billed, freight_concession, freight_expense, vendor_rebate, inbound_freight, duty, credit_reason, original_invoice_ref, sales_rep_id, channel, ship_to_id, bill_to_id, parent_account_id, contract_id, currency, item_category, item_family, supplier_id, weight, cube, handling_units, kit_parent_id, item_role`

**Optional reference tables**
Price list history (effective-dated), UOM conversions, FX rates, rebate program definitions, contracts and price-protection periods, quotes with outcomes, substitution groups, inventory receipts, cost-to-serve activity rates.

### 5.3 Transaction classification
| Case | Treatment |
|---|---|
| Ordinary sale | Included in sales observations and the financial ledger |
| Return | Excluded from sales observations; financial effect kept in the ledger and linked to the original sale when possible |
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
- Target and start are **provisional** (labeled `provisional_historical`) until Phase 7 can support optimization.
- Line statuses: `in_band`, `below_reference`, `below_floor`, `above_reference`, `insufficient_pricing_headroom` (corridor infeasible against the floor), `insufficient_data`.
- **Verify:** floor math fixtures (e.g. cost $100, m = 5% → $105.26 pocket floor); infeasible corridors flagged, never relabeled.

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

### RUN 3 — API, UI, agent

#### Phase 18 — API (FastAPI)
- Endpoints to upload data and mapping, run pipeline, fetch results and manifest. Pydantic response models. Tests.

#### Phase 19 — UI (React + TypeScript + Vite)
- Views: data quality and coverage, waterfall, dispersion, segments, price guidance lookup, opportunity, scenario builder.
- Evidence status and data coverage visible on every model output.

#### Phase 20 — Explanation agent (optional, Claude API)
- Explains a recommendation in plain English using **only computed outputs and their evidence status**. Never invents numbers. Engine runs fully without it.

---

## 7. Out of scope
- Real-time ERP integration
- Multi-user auth, hosting, billing
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
| Allocation residual | Assigned deterministically to the largest line in the group |

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
| Target / start | Provisional (P50 / P75, never below floor) until optimization is supported |

### 8.6 Sensitivity
| Item | Default |
|---|---|
| Panel period | Month |
| Active assortment (zero rows) | Pair purchased in the prior 6 months and item available |
| First-stage strength | Effective F ≥ 10 (rule-of-thumb threshold, configurable) |
| Clustering | Level where the instrument varies (supplier or category × month) |
| Shrinkage | Toward parent commercial segment |
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
`schema, mapping, units_and_currency, transactions, costing, waterfall, outliers, segmentation, price_guidance, sensitivity, opportunity, tariff_scenarios, reporting`
