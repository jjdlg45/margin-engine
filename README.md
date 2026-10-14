# Margin Engine

An auditable pricing and margin decision engine for B2B distributors
(industrial, electrical, HVAC, plumbing, foodservice — any business with
negotiated prices). It ingests ERP invoice exports, reconciles every dollar,
and produces a price waterfall, peer-group dispersion with outlier flags,
commercial segments, reference-band price guidance with an economic floor,
instrumented price-sensitivity estimates with explicit evidence statuses, and
opportunity sizing — each output tied to a dollar figure or a decision, with
data coverage and evidence status always visible.

## The problem

Distributor pricing data is messy (returns, credit memos, duplicates, missing
costs, mixed units) and endogenous (reps discount when demand is weak, so
realized prices correlate with unobserved demand). Tools that ignore the first
produce numbers that don't reconcile; tools that ignore the second recommend
price increases justified by elasticities biased toward zero. This engine is
built around both problems:

1. **Accounting first.** Every revenue, deduction and cost reconciles to
   source totals, exactly, in integer cents. No dollar is silently dropped and
   nothing is forced onto lines to make a report look complete.
2. **Proven, not claimed.** A synthetic-data generator plants known truth
   (segments, elasticities, rebates, outliers, endogeneity, instruments) and
   the test suite measures recovery across random seeds.
3. **Fail closed.** Where the data does not support a result, the output says
   `insufficient_evidence` (or a lower evidence status) instead of a
   confident-looking number.

## Method in one paragraph

A mapping YAML declares column names AND business meaning (sign conventions,
cost basis and inclusions, date semantics); ingestion fails actionably on
missing semantics rather than guessing. Classified transactions produce two
views — clean sales observations and a complete financial ledger — and a
seven-stage waterfall (list -> invoice -> pocket revenue -> pocket gross profit
-> contribution) with exact allocations. Prices normalize to an index against
the effective-dated list; robust z-scores flag data-quality anomalies and
commercial outliers separately. Commercial segments come from a decision tree
on the price index using context features only (leakage-guarded), consolidated
CHAID-style and stabilized by bootstrap partition agreement. Price guidance is
historical reference bands (P25/P50/P75) against an exact economic margin
floor — labeled provisional, because historical benchmarks are not optimal
prices. Own-price elasticity is estimated per segment by fixed-effects 2SLS
with two observed instruments (supplier cost index, category tariff rate),
with first-stage, overidentification, robustness and weak-IV-robust (Anderson-
Rubin) diagnostics feeding a four-level evidence status. Opportunity is sized
at three levels that are never summed, and cost/tariff scenarios show 0/50/100%
pass-through side by side. Full methodology: `docs/methodology.md`.

## Validation results (planted truth vs. recovered)

All targets measured on synthetic data at default generator settings across
multiple random seeds (see `docs/build-log.md` for the full record and
`tests/` for the measurements themselves).

| Check | Target | Measured |
|---|---|---|
| Accounting fixtures (13 hand-calculable cases) | Exact to the cent | Exact |
| Ledger reconciliation to source totals | Exact after rounding | Exact (7 checks, every run) |
| Rebate / freight recovery — actual amounts | Exact | Exact |
| Rebate / freight recovery — allocated amounts | Within ±5% of planted | Within ±5% per customer |
| Planted outlier detection (seeds 5/17/29) | ≥ 90% recall, ≤ 5% false flags | 98.8–99.3% recall, 0.9–1.1% false flags |
| Commercial segments vs. planted, out-of-sample ARI (5 seeds) | ≥ 0.70 | 0.940–1.000 |
| Segment bootstrap stability, median ARI | ≥ 0.80 | 0.923–0.985 |
| Elasticity recovery (planted-segment grain), 5 seeds × 6 segments | median ≤ 0.10, P90 ≤ 0.15 | median 0.052, P90 0.124, worst 0.204 (reported) |
| Elasticity recovery (end-to-end, engine segments, purity ≥ 0.9 cells) | median ≤ 0.10, P90 ≤ 0.15 | median 0.055, P90 0.135, worst 0.290 (reported) |
| `realistic_noise` preset (Poisson quantities, pre-tightening noise) | informational, not gated | median 0.131, P90 0.220, worst 0.263 |
| 95% CI coverage, 20 simulations (120 estimates) | 90–98% | 92.5% |
| Naive estimator bias | Toward zero on every seed | Toward zero on every seed and segment |
| `weak_instrument` preset | Never causal-supported; weak-IV-robust CI reported | Holds (AR set reported, incl. empty-set outcome) |
| `invalid_instrument` preset (5 seeds) | Overidentification rejects ≥ 80% of seeds, status downgraded | Rejects 5/5, downgraded |
| `price_constant` / `sparse` presets | Never above descriptive | Holds |
| Performance, full pipeline, 597,673 lines | Under 2 minutes | 40.1s, peak memory 3.97 GB (AMD64 Family 25 / Ryzen, Windows 11; stage timings in the run manifest) |

Elasticity recovery is gated at both grains: the planted-segment grain (where
the truth is defined) and end-to-end on engine-recovered segments, where each
engine cell is compared against the planted elasticity of its modal planted
segment (cells mixing planted segments below 0.9 purity have no single truth
and are excluded from the comparison). The `realistic_noise` row shows the same
estimator on a deliberately harder world — Poisson count quantities, where the
planted beta is only approximately the log-log estimand, and higher noise — as
an honest indication of degradation; it is reported, not gated.

## Run it

```bash
uv sync

# Generate a synthetic world (deterministic per seed)
uv run python -m synth.generate --out data/demo --seed 42

# Run the full pipeline
uv run python -m margin_engine.cli run \
    --data data/demo/erp \
    --mapping config/mappings/synth_erp.yaml \
    --rules config/rules.yaml \
    --out output

# open output/report.html; tables are also in output/*.parquet and *.csv
```

Quality checks: `uv run ruff check . && uv run mypy src && uv run python -m pytest`
(on Windows machines with Smart App Control, use `python -m pytest`, not the
`pytest` shim).

## Running on real data

1. Copy `config/mappings/synth_erp.yaml` and edit it for your export: column
   names, date formats, transaction codes, and — critically — the semantics:
   the sign convention of every amount column, whether discounts are embedded,
   the cost basis and what it includes, and the unit basis of the cost.
2. Point `--data` at the directory with your CSV export and reference tables
   (price list, rebate programs/settlements, order charges, freight log, UOM
   conversions, supplier cost index, tariff schedule — each optional; absent
   tables switch their features off and the report says so).
3. The engine will refuse to compute anything whose semantics you did not
   declare, and will tell you exactly which declaration is missing and why.

No code changes are needed for a new source system.

## Honest labels and limitations

- Historical reference bands are not optimal prices; targets are labeled
  `provisional_historical` unless causal elasticity support allows
  optimization.
- Descriptive elasticities are not causal. The naive FE-OLS estimate is
  reported to document endogeneity bias, never used for decisions.
- The overidentification test only detects an invalid instrument when the two
  instruments disagree. Exclusion restrictions remain economic assumptions —
  documented per instrument in `config/rules.yaml` — that no statistical test
  fully proves.
- Allocated amounts are labeled `allocated`, never presented as actuals;
  unallocatable amounts stay as visible residuals.
- Item availability for the demand panel is proxied by market-wide activity
  (stockouts are not directly observable in invoice data); the proxy is
  labeled.
- Validation demonstrates recovery of planted truth in synthetic worlds. It
  does not demonstrate real-world accuracy beyond what those tests measure.

## Roadmap

- Run 2: multi-currency and UOM, customer hierarchies and contracts, tiered
  retrospective rebates, kits and free goods, cost-to-serve activity rates,
  inventory costing timing, product roles / KVI candidates, quotes and
  win/loss, substitution and cross-price effects.
- Run 3: FastAPI service, React UI, optional plain-English explanation agent
  (computed outputs only; the engine runs fully without it).

## Repository layout

See `CLAUDE.md` section 4. Key entry points: `margin_engine.cli` (CLI),
`margin_engine.report.full_run` (pipeline orchestration),
`synth.generate` (synthetic world generator), `tests/` (validation suite).
