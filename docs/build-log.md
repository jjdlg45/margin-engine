# Build log

Chronological record of each phase: what was built, verification vs. section 8
targets, decisions, known gaps. Appended after every phase per CLAUDE.md.

---

## Phase 0 — Scaffold and tooling (2026-10-13)

**Built**
- uv project pinned to Python 3.12.15 (system Python is 3.14; `.python-version` pins 3.12).
- Full dependency set installed: polars, duckdb, pandera[polars], pydantic, pyyaml,
  scikit-learn, pyfixest, linearmodels, statsmodels, typer, jinja2, numpy, pyarrow;
  dev: ruff, mypy, pytest, pytest-cov, hypothesis, pre-commit.
- Repo structure from spec section 4 (all `src/margin_engine/` submodules, `synth/`,
  `tests/fixtures`, `tests/property`, `config/mappings`, `data/sample`, `docs/`).
- `src/margin_engine/config.py`: pydantic models for all 13 `rules.yaml` sections
  (schema, mapping, units_and_currency, transactions, costing, waterfall, outliers,
  segmentation, price_guidance, sensitivity, opportunity, tariff_scenarios, reporting),
  frozen + `extra="forbid"` so unknown keys fail at load. Defaults encode section 8
  (z 3.5, peer-group min 30, tree depths 5/4, leaf minimums 500/0.5%/20 customers/10 items,
  5% pocket-margin floor as Decimal, F ≥ 10, lookback 6 months, baseline 12 months,
  pass-through 0/50/100%).
- `config/rules.yaml` default policy file, including both instruments with documented
  exclusion rationales (required later for `causal_elasticity_supported`).
- Typer CLI with `version` and `validate-config`; `run` arrives in Phase 8.
- GitHub Actions CI (lint, format check, mypy, pytest+cov), pre-commit config, .gitignore.

**Verification**
- `uv sync`: pass. `ruff check` + `ruff format`: pass. `mypy src` (strict on
  `margin_engine.*`): pass, 0 issues. `pytest`: 9/9 pass (config schema tests:
  defaults match spec, unknown keys rejected, invalid values rejected, Decimal type
  enforced for margin floor). CI and pre-commit YAML parse cleanly.

**Decisions**
- Windows Application Control blocks the `pytest.exe` shim in `.venv/Scripts`; all
  local test runs use `uv run python -m pytest` (identical behavior). CI on Ubuntu
  is unaffected.
- mypy strict scoping done via `[[tool.mypy.overrides]] module = "margin_engine.*"
  strict = true` rather than global strict, so tests/synth get normal checking.
- `min_pocket_margin` parses from a YAML string ("0.05") into Decimal to keep the
  money-is-exact rule even in config.

**Known gaps**
- mypy notes unused `ignore_missing_imports` override sections for libraries not yet
  imported (pandera, sklearn, pyfixest, linearmodels, statsmodels, scipy, duckdb) —
  they activate as later phases import them.

---
## Phase 1 — Synthetic generator with planted truth (2026-10-13)

**Built**
- `synth/` package: `config.py` (GeneratorConfig + 5 scenario presets), `entities.py`
  (customers, items, reps, 6 planted segments, effective-dated list price history,
  contracts, rebate programs, UOM conversions), `demand.py` (structural model),
  `lines.py` (invoice lines, exact deduction math), `mess.py` (11 mess types),
  `output.py` (ERP-styled export + ground truth), `generate.py` (orchestrator + CLI),
  `money.py` (exact integer-cents half-even division; no float pass).
- Structural model: log price index = segment base + pair effect
  + 0.6·(cost shock + tariff shock) [instruments] + 0.08·demand shock [endogeneity]
  + rep targeting of price-sensitive customers; demand is Poisson with planted
  segment elasticity. Naive bias direction (toward zero) recorded in ground truth.
- Contract pairs get fixed protected pricing (no shocks, no discounting).
- Mess: linked partial returns, credit memos with reasons, zero-cost service items,
  missing costs (truth kept), alternate-UOM re-expression (net amount unchanged so
  reconciliation stays exact), verified duplicates (same source key) and look-alike
  duplicates (new key), data-entry price outliers (x10, /10), commercial outliers
  (extra 25% discount), malformed zero-qty rows, stockouts.
- Export: `erp/invoice_lines.csv` with deliberately non-canonical column names
  (LINE_KEY, INV_NO, QTY_SHP, EXT_NET, UNIT_CST, TXN_CD...), 13 reference tables
  (price list, settlements, order charges, freight log, supplier cost index, tariff
  schedule, landed components...), `ground_truth/` (truth_summary.json,
  line_truth.parquet, panel_truth.parquet with full availability panel).

**Verification (vs. Phase 1 criteria)**
- Same seed → byte-identical CSVs and identical truth summary: PASS.
- Every mess type appears; rates within ±50% of configured: PASS.
- Preset conditions: well_identified first-stage slope ≈ 0.6 (> 0.3): PASS;
  weak_instrument slope < 0.15: PASS; invalid_instrument tariff→demand leak
  detectable (corr > 0.05) and absent in baseline: PASS; sparse produces < 8k lines
  at test scale: PASS; price_constant has exactly zero within-pair price variation
  (list revisions disabled in that preset): PASS.
- Endogeneity: within-pair corr(price, demand shock) > 0.05: PASS.
- Returns all linked to existing invoices, negative qty: PASS. Credits all carry
  reasons: PASS. Settlement table == sum of planted line truth to the cent: PASS.
- Scale: 597,968 lines (2,200 customers / 1,800 items) generated successfully.
- ruff, mypy, pytest: all pass (26 tests).

**Decisions**
- Ground-truth totals and reference-table aggregates computed on the deduped frame;
  verified-duplicate copies are export artifacts, not additional dollars.
- Price corruption (outliers) applied BEFORE deduction truths so rebates/freight stay
  internally consistent with corrupted prices, as in a real ERP.
- price_constant preset also disables list revisions, so prices are truly constant.
- Settlement-rebate allocation test will be near-trivially exact (allocation basis
  matches generation); noted so the ±5% target isn't mistaken for a strong test there.
  Freight expense allocation is the non-trivial one (fixed + variable components).

**Known gaps**
- Commercial outliers are price-only modifications (no demand response); acceptable
  for detection tests, noted for honesty.

---
## Phase 2 — Ingest, classification, ledger, waterfall, reconciliation (2026-10-13)

**Built**
- `ingest/mapping.py`: mapping schema (pydantic, extra=forbid). Every amount field
  declares currency + sign convention; unit_cost additionally requires cost_basis,
  three inclusion flags, and per_uom (line vs. base unit). Missing semantics raise
  MappingError naming the field, the missing semantic and the calculation that needs
  it. `config/mappings/synth_erp.yaml` maps the generator's export.
- `money.py`: digit-exact string->Int64 parsing at scale 1e-4 (no float pass),
  refusal to truncate >4dp values, half-even integer division as polars exprs,
  Decimal conversion at report boundaries. All internal money is Int64 cents.
- `ingest/loader.py`: mapping-driven CSV->canonical frame; actionable errors for
  missing columns, bad dates, unparseable money, undeclared transaction codes;
  comma-decimal support; net computed from authoritative unit price when mapped.
- `transactions/classify.py`: verified-duplicate removal (same key AND identical
  content; key collisions flagged, kept), look-alike flagging (kept), credit
  classification by configured reasons, all 5.3 flags. Two views per 5.4.
- `waterfall/allocate.py`: exact proportional allocator, integer math, residual
  deterministically to the largest line; signed-driver mode for settlements so
  returns carry negative rebate shares; unallocatable groups left as visible residual.
- `waterfall/engine.py`: 7-stage waterfall. UOM normalization (missing conversions =
  actionable error), effective-dated gross list (list never manufactured from
  invoice price), billed charges -> invoice revenue (allocated), rebates/concessions/
  price credits -> pocket (credit dollars count exactly once: attributed credits zero
  on the credit row), landed cost build-up per declared semantics, vendor rebates net
  against base cost, freight expense + claims -> cost-to-serve, contribution.
- `waterfall/reconcile.py`: 7 exact checks (row/net conservation incl. removed dups,
  allocation conservation for charges/settlements/freight, stage identities); any
  failure raises. `ledger/pipeline.py`: single entry point + DuckDB persistence with
  source_line_key audit trail (parquet handoff; works where pyarrow DLLs are blocked).

**Verification vs. section 8**
- 11 deterministic fixtures exact to the cent (normal sale, stacked discounts,
  line + settlement rebates, vendor rebate, freight concession vs. expense, partial
  return, price-correction credit, damage credit to cost, zero-cost service, missing
  cost, landed build-up, UOM normalization, billed freight): PASS.
- Property tests (hypothesis, 25 examples x random qty/price/sign/deductions):
  reconciliation always exact: PASS.
- Mapping tests: equivalent data via two different layouts -> identical canonical
  frames; 10 failure modes all actionable: PASS.
- Recovery vs. planted truth (seed 11 world): line rebates exact; concessions exact;
  vendor rebates exact (cost-known lines); billed freight exact per order;
  settlement rebates and freight expense allocated within ±5% per customer: PASS.
- Ledger reconciliation on full synth world exact; negative settlement residual
  (-$68.17) is return-only customer-quarters, visible in residual report: PASS.
- ruff, mypy (strict), pytest 66 passed / 1 skipped (Phase 5 floor placeholder).

**Decisions**
- Money as Int64 cents internally (spec allows Decimal or integer cents); Decimal at
  boundaries. Parsing is digit-wise, never through float.
- Settlement rebates allocate over sales AND returns with signed weights (matches
  accrual economics); groups with non-positive driver sum stay unallocated residual.
- Vendor rebates apply to base purchase cost, not freight/duty.
- Gross list value comes from the effective-dated price_list reference x normalized
  quantity; the line-level list column only feeds the missing-list flag.
- DuckDB ingestion via polars-written parquet (pyarrow DLL blocked on this machine).

**Known gaps**
- Billed-freight allocation driver is net share (weight driver arrives with Phase 13
  cost-to-serve data); order totals remain exact either way.

---
## Phase 3 — Descriptive analytics (2026-10-13)

**Built**
- `dispersion/analytics.py`: normalized price index (price / effective-dated list,
  item-median fallback, method labeled per line); peer groups item x UOM x contract
  regime x quarter with family fallback below 30 observations (level labeled);
  modified robust z-score (0.6745 x dev / MAD) at 3.5; MAD = 0 fallback flags >10%
  deviation from the modal price (method labeled); degenerate cohorts (<5 lines)
  labeled, never flagged; two mutually exclusive flags (data_quality_anomaly when
  the price is >=3x or <=1/3 of the peer median, else commercial_pricing_outlier);
  peer-group dispersion stats (median, IQR, P10/P50/P90, discount-to-list);
  three separate ABC splits (revenue, units, order frequency) at 80/15/5.

**Verification vs. section 8**
- Planted outlier detection across seeds 5/17/29: recall 97.2% / 96.2% / 94.9%
  (target >= 90%); false-flag rate 0.8% / 0.5% / 0.7% (target <= 5%): PASS.
- Data-quality outliers classified as data_quality >= 90%, never double-flagged: PASS.
- MAD = 0 fallback: dedicated tests (deviant flagged, method labeled, entry-error
  magnitude classified as data quality): PASS.
- Flags never mixed (zero rows with both): PASS. Originals preserved: PASS.
- ABC: revenue-A share in [0.70, 0.85]; three versions differ: PASS.
- ruff, mypy, pytest (77 passed, 1 skipped): PASS.

**Decisions**
- Planted commercial outliers strengthened from a 25% to a 50-60% one-off discount:
  at 25% they sit ~2.3 robust-z inside normal peer dispersion, i.e. they are not
  outliers under the spec's own definition. The generator now plants genuinely
  extreme-but-plausible discounts (clearance/strategic), and the 3.5 threshold was
  NOT loosened.
- data_quality vs commercial classification threshold (3x / 1/3 of peer median) is
  a module constant; planted errors are x10 / /10 so separation is clean.
