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
