# Run 4 report — correct, real-data-ready, and live

Branch `run4`, phases 24–31, one commit per phase, never pushed. Full detail
per phase lives in [build-log.md](build-log.md); this is the owner's summary:
what changed, the before/after numbers, the known gaps, and the decisions
waiting on you. All checks green at the final commit: ruff, ruff format, mypy
strict (src + api), pytest (339 tests, slow/network tiers deselected by
default), tsc, vitest, vite build, Playwright 144/144 static + 4/4 API mode.

## Phase 24 — Logic remediation

Fourteen audit items. The ones that moved money:

- **Pocket-aware sizing (24.1/24.2/24.3).** Opportunity contribution now uses
  P·(1−d) − F − C with the pocket deduction rate, missing cost is never
  zero-filled, and guidance gained an optional contribution floor plus the
  `market_reference_below_floor` status. Demo-world effect: gross opportunity
  fell from $478,516.90 (invoice basis) to $404,500.15 (pocket basis) — the
  pocket conversion is most of the drop, and the old number was overstated.
- **Stricter causal gating (24.5–24.8).** Two-way month × customer clustering
  (chosen by a 50-simulation coverage study: 93.3% vs 83.3%/82.3% for the
  alternatives), an extrapolation guard (`outside_price_support`: 122 of 660
  records reclassified to fixed-volume on the probe world), Anderson-Rubin
  boundedness/width criteria, and a carried-forward-price robustness check.
  Expected contribution change fell from $284,364.28 (pre-Run-4 gate) to
  $77,996.86 on the new demo world — less money claimed, far better defended.
- Also: LIFO costing, conservative below-P25→P25 level ($114,784.71 on the
  demo), GAAP labels ("net invoiced revenue") and an ASC 606/330/705-20
  mapping, misspecified validation worlds, the competitor co-movement control
  and its documented limitation.

## Phases 25–26 — Filters and the live-ready data layer

25 was delivered pre-Run-4 (as 23.8) and re-verified on the branch. 26 added
the missing piece: an HTTP adapter implementing the same DataClient contract,
selected by `VITE_DATA_SOURCE=http` — anything else means static, so the
hosted demo can never silently depend on a server. One latent React Aria bug
fixed (Escape cleared multi-select filters).

## Phase 27 — Real-data readiness

- **Profiler + draft mapping with a CONFIRM protocol**: the engine refuses to
  run a mapping with unconfirmed guesses.
- **Books tie-out** against `gl_totals` with dual tolerance; failures flagged,
  never hidden.
- **Validation without planted truth** (backtest, time-permutation placebos,
  rolling stability) in every run and report. Two method decisions worth
  knowing: the 2SLS ratio is NOT a valid placebo statistic (with an
  irrelevant instrument it concentrates around OLS), so placebos judge the
  reduced form and first stage; and instrument shuffles must permute in time
  within unit, because cross-unit shuffles stay contaminated through month
  demeaning when units are few.
- **Anonymizer** (salted keyed hashes, optional secret money scaling, salt
  never stored).
- **Instrument builder**: supplier cost index from receipt history
  (mix-corrected: per-item demeaning first, otherwise item mix swamps the
  signal — the naive version recovered planted shocks at r=0.14, the
  corrected one at r>0.9), or from an external public series.
- **Scenario matrix**: ~30 fast worlds in CI, 300-world slow tier. The matrix
  caught five real bugs (margin-tree crash without list price, crash without
  instrument columns, decimal-separator miss on `market_index`, null-dtype
  elasticity crash, and a genuine nondeterminism — polars switching
  divide/reciprocal kernels between runs — pinned by a lossless round).
  Final full tier: **300/300 worlds pass every invariant**.

## Phase 28 — Case study on real data (Online Retail II)

1,067,371 real invoice lines (UCI, CC BY 4.0), run end to end in 152 s at
4.2 GB peak with exact reconciliation: $30.40M USD net invoiced revenue
(£19.29M source), 19,494 cancellations classified by a mapping prefix rule,
242,257 anonymous sale lines handled (in the ledger, out of behavior views),
67,250 look-alike duplicate flags. Elasticity correctly caps at
`descriptive_elasticity`, and the Phase 27 no-truth checks make the case
(backtest below the zero-elasticity baseline; fake-date placebos 80–100%
"significant" — the panel violates the no-effect null). Level-2 opportunity:
$698,667 on the labeled index-fallback price basis (conservative variant
$566,992); expected-change level honestly absent. Eight real-data gaps were
found and fixed (anonymous customers, blank keys, invoice-as-order proxy,
always-present optional columns, minimal items tables, FX conversion of
derived net amounts, list-free level-2 sizing, no-cost declaration), each
pinned by the committed 5,000-line sample's tests.
Write-up: [case-study-online-retail.md](case-study-online-retail.md).

## Phase 29 — External connectors (free, keyless)

Frankfurter FX, BLS PPI v1 (25 queries/day limit enforced client-side) and
the USITC HTS export — all verified, cached, rate-limited, stale-labeled,
offline-tested from recorded real fixtures
([data-sources.md](data-sources.md)). HTS serves the current revision only,
so its rates feed pass-through scenarios and never the tariff instrument.
Hybrid world: real 2024–2025 PPI movements drive the cost instrument; median
abs elasticity error **0.037**, 4 of 6 segments causal-supported
(informational).

## Phase 30 — BFF API and live mode

FastAPI service implementing the Phase 26 OpenAPI contract exactly (filter/
sort/pagination ported line-for-line from the static adapter; parity proven
cents-exact in tests), DuckDB-backed bundle versions with ETag/304s, inbound
token-bucket rate limiting (429 + Retry-After), scheduled refreshes through
the connectors only, and an honest replay feed (Online Retail II one business
day per tick, idempotent by construction, labeled "Historical data replayed"
with "Data as of" and per-section stale notices in the UI). One-command local
run: `uv run python scripts/live_mode.py` (Docker unavailable on this
machine; the spec's documented uv+npm alternative). Hosting documented, not
executed, for Render free tier and Oracle Always Free
([deploy-api.md](deploy-api.md)).

## Test growth

248 tests at the start of Run 4 → **339** at the end (plus the 300-world slow
tier and a `network` marker group), including the 30-world matrix fast tier,
7 Online Retail sample tests, 9 connector tests, 11 API tests, 7
real-validation tests, and 4 API-mode Playwright tests on top of the 144
static ones.

## Known gaps

1. **Scale (27.2) — the one FAIL in validation.json.** The 10M-line /
   8 GB-peak target is unverifiable on this 13.8 GB laptop (~5 GB free during
   runs). The memory-aware ladder records the largest run that fits (241k
   lines, 2.92 GB; an earlier higher-headroom run reached 811k lines at
   6.42 GB). Per-stage RSS shows the memory is structural (several wide
   line-level frames alive at once), so hitting 10M-under-8GB needs the
   DuckDB-backed/streaming refactor the spec names — deferred because no
   result on this machine could verify it, recorded honestly instead of
   loosened.
2. **HTS has no history**, so the tariff instrument still requires an
   effective-dated tariff table; HTS feeds scenarios only.
3. **Docker compose not provided** (Docker unavailable here); the documented
   `scripts/live_mode.py` fills 30.6's alternative. A compose file would be
   untested, so it was not committed.
4. **Fail-closed scope in the matrix**: `heterogeneous_elasticity`,
   `nonconstant_elasticity` and `persistent_demand` are treated as
   identifiable-with-caveats (their recovered estimand is a documented
   average, consistent with the committed 24.9 results), so causal support
   there is not an invariant violation; `competitor_comovement` is in the
   fail-closed set and holds.

## Decisions waiting on the owner

1. **Review and merge `run4`** (nothing was pushed). The public demo deploys
   from `main` and is unchanged until then.
2. **Scale follow-up**: accept the KNOWN GAP as recorded, or commission the
   streaming refactor plus a run on a machine with ≥ 16 GB free to verify the
   10M target.
3. **Live mode hosting**: whether to deploy the API per
   [deploy-api.md](deploy-api.md) (Render free tier limitations vs an Oracle
   VM) — the GitHub Pages demo stays static either way.
4. **Replay source for a hosted demo**: committed 5,000-line sample (runs in
   seconds, limited story) vs the full dataset (needs the Oracle-class
   option; ~2.5 min per replay tick).
