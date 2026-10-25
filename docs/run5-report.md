# Run 5 report — Scale, compliance, and the Action center

Branch `run5`, phases 35–47, executed continuously per the Run 5 spec in
CLAUDE.md. Every phase ran the applicable Python checks (ruff, ruff format,
mypy strict on `src` and `api`, full pytest) and web checks (tsc, vitest,
production build, Playwright static and API-mode suites) with exit codes
confirmed before its commit, and has a detailed entry in
[build-log.md](build-log.md). This report is the owner-facing summary:
what changed, the before/after numbers, the known gaps, and the decisions
waiting on the owner. **Nothing on this branch is merged or pushed; the
public demo on `main` is untouched.**

## Headline numbers

| Measure | Before Run 5 | After Run 5 |
|---|---|---|
| Default run, 241,162 lines (300×500 world, same machine) | 32.1 s, 2.92 GB peak | 13.0 s, 2.64 GB peak |
| Peak bytes per line at that rung | 13,001 | 11,754 |
| Marginal bytes per line between rungs | 9,972 | 8,987 |
| Engine exports across the whole scale refactor | — | Byte-identical (sample 46/46 files, world42 44/44, demo 46/46) |
| Proposed actions, committed sample world | — | 288 (73 below-floor, 200 price changes at the per-type cap, 2 pass-through, 3 service policy, 10 rebate reviews; 11 carry compliance flags) |
| Proposed actions, demo bundle | — | 277, read-only with simulated lifecycle-valid history; 13 outcomes measured for real against the bundle's ledger |
| Results-tracker recovery (30 planted +8% interventions, well-identified world) | — | 21/30 measured; pooled DiD recovery error 0.039 log points vs a 0.10 bound (per-pair median 0.26 — irreducible 90-day Poisson noise, reported) |
| Results tracker on a sparse world | — | 0/30 measured, every refusal with its reason — fail-closed |
| Dependency audits | not gated | pip-audit clean over the frozen lock; npm audit clean at the high gate (5 reviewed moderates accepted in writing in docs/security.md) |

## Phase-by-phase

### Phase 35 — Scale refactor

The engine's RAM per line and runtime were cut without moving a single
exported byte; byte-identity was enforced mechanically (every CSV and parquet
hashed on three worlds before the refactor and after every sub-change). What
changed: real-data validation moved behind `run --validate` and a new
`validate` command (it was 63% of default wall time); ingest became a lazy
streaming scan with validation as one aggregation pass; the waterfall's
allocations now run on narrow projections joined back by a run-scoped row id
instead of splitting the full-width ledger per allocation; 24 low-cardinality
code/status columns are Categorical in memory (exports cast back, bytes
unchanged); exports write-and-release one frame at a time with CSVs streamed;
the DuckDB store reuses the just-written parquet files and runs under a 1 GB
memory limit; a content-addressed stage cache (`run --cache-dir`, used by the
replay feed, which now parses its source exactly once). Tests added: cache
roundtrip/key-sensitivity/torn-write/poisoned-hit, replay single-parse,
`--validate` flag and command.

Numbers: 32.1 s → 13.0 s and 2.92 → 2.64 GB at 241k lines; marginal memory
9,972 → 8,987 bytes/line. The 10M-line target remains a KNOWN GAP (below).

### Phase 36 — Fiscal calendars

One configurable calendar (`fiscal` config section) drives every analytic
period: calendar months (default — byte-identical to the historical
expressions, verified), 4-4-5 / 4-5-4 / 5-4-4 week calendars
(weekday-nearest-year-end convention, 53rd week extends the final period,
"FY2026 P03" labels), or a custom period table validated for overlaps and
coverage. Data-declared periods (rebate settlements, landed-cost months, FX
months) deliberately stay as the source states them. 11 tests, including a
programmatically found 53-week year and a full pipeline run under 4-4-5 with
exact reconciliation.

### Phase 37 — Pricing compliance

Robinson-Patman review flag: likely-competing customers (same configured
attributes, default industry + region) whose recommended prices for the same
item differ by strictly more than the threshold (default 5%) are flagged, with
the cost-to-serve difference shown beside every flag because cost
justification is the first defense a reviewer checks. New
`compliance_robinson_patman` export and `rp_review_flag` on guidance and
opportunities. The market/competitor price index is accepted as an elasticity
control only when the mapping declares `public_source: true`; otherwise the
control switches off with a stated note. Methodology §11 (orientation, not
legal advice). 5 tests, including the exact threshold boundary and the
end-to-end public-source gate.

### Phase 38 — Action model and generation

`margin_engine/actions/`: lifecycle as an append-only event log with status
derived by replay and tamper-catching log validation; five deterministic
action types with impact at the right opportunity level per type (never
summed); guardrails (floor violations raise, contract-protected pairs generate
blocked actions, compliance flags travel with the action); DuckDB store with
idempotent seeding; simulated-but-valid demo history for the static site.
9 tests (determinism, traceability of every action to its source records,
lifecycle replay and refusals, store enforcement).

### Phase 39 — Maker-checker review

Roles analyst/approver/viewer with role gates per transition; separation of
duties (no self-approval); written justification required on flagged
approvals; whitelisted, audited, floor-checked edits frozen after approval;
`merge_new` so a fresh engine run never touches existing review history. Full
API surface (queue, summary, detail, one endpoint per transition, bulk
approve with per-item refusals, propose, edit) in the OpenAPI contract.
Identity is a configured user name — documented everywhere as not
authentication. 8 HTTP-driven tests.

### Phase 40 (+40.1) — ERP price export

Approved price actions become a configurable, deterministic, idempotent
ERP-importable price file with a per-basis diff summary (never one summed
total); CLI `export-prices` and an API endpoint. 7 tests, including a
round-trip re-import through the engine's own ingest exact to the cent.
Phase 40.1 records a process failure honestly: the phase-40 commit landed
before its background check finished, and the check then failed (stale mypy
ignores; a replay test crashing on a pre-upgrade action store). Fixed with a
forward schema migration in the store (an upgrade never crashes and never
loses review history), test hermeticity, and a process change — later phases
gate the commit on exit codes read from the check log.

### Phase 41 — Results tracking

Price realization, difference-in-differences volume against the prediction
stored on the action at generation time, and margin before/after, all gated
fail-closed with reasons; calibration by evidence bucket appears only past a
minimum count. The generator gained planted price interventions, and
`scripts/measure_tracker.py` recorded recovery in validation.json: pooled
error 0.039 log points (bound 0.10) with 21/30 measured on the
well-identified world; 0/30 measured with reasons on the sparse world. The
honest finding: a single pair's 90-day window carries ~0.26 log points of
Poisson noise, larger than a realistic planted effect — so the tracker's
per-action volume verdict is weak evidence by design, and the pooled
calibration is the meaningful read (documented in methodology §12). 5 tests
plus generator coverage.

### Phase 42 — Action center UI

New `/actions` route and detail pages on the shared filter system: summary
cards (impact by basis, never summed), queue, results tab with calibration,
detail with rationale, compliance explanation, evidence deep links,
append-only history and outcome cards. Static demo is read-only (every
mutation rejects); API mode adds the identity picker, lifecycle buttons with
optimistic updates that roll back on failure, manual proposals, and the
price-export download. Bundle files stay under the 1 MB gate. Tests: bundle
parity cents-exact, 12 static Playwright passes, 9 API-mode passes (full
lifecycle through the UI, role refusals, justification flow, rollback on a
simulated 500).

### Phase 43 — Drift monitoring

Each live-mode refresh compares against the previous run: data-quality rates,
segment assignments (ARI over shared lines), per-segment elasticity estimates
and statuses, and guidance status counts (relative and absolute gates). Moves
beyond thresholds become plain-English flags served on `/api/drift` and shown
in the UI shell; a reconciliation regression always flags. 7 planted-shift
tests, including a genuine-partition-change ARI test (not a label
permutation) and exact threshold boundaries.

### Phase 44 — Supply-chain security

Dependabot (uv, npm, actions); CI gates pip-audit (frozen lock, any known
vulnerability fails) and npm audit at the high level. Turning the gates on
surfaced real findings, all fixed: pillow, virtualenv, filelock;
react-router-dom 6.30.6, postcss, Playwright 1.55, vitest 5 (tinypool
criticals), vite 7.3.7 (`vite@latest` tried and reverted — the rolldown line
rejects this config). Five remaining npm moderates are reviewed and accepted
in writing in docs/security.md; an escalation to high re-breaks CI.

### Phase 45 — Container

Multi-stage Dockerfile (web build → uv-based API with in-process scheduler →
nginx UI with `/api` proxied) and a compose file with a one-shot demo-export
job and a persistent state volume. CI builds the images and smoke-tests the
composed stack on every push; this machine has no Docker, so the proof runs
on GitHub's runners (stated, not hidden).

### Phase 46 — Display polish

Categorical data values render reader-facing with word capitalization and
mapping-configured acronyms ("OEM", never "Oem"); raw exports stay raw;
engine-constructed band levels deliberately keep sentence case (two existing
phrasing tests caught the first over-broad pass — fixed, tests intact). New
Playwright rendering spec proves every guidance, sensitivity, opportunity and
action status in the data renders with its dictionary label and plain-English
explanation, desktop and mobile.

### Phase 47 — Documentation and verification

README (Action center, fiscal calendars, compliance flag, scale results,
validation table refreshed from validation.json), methodology §12 (action
generation rules, guardrails, the results-tracking method and its limits),
this report, and the CI-like verification below.

## Known gaps

- **10M-line target (27.2) — still a KNOWN GAP.** The memory-aware ladder
  records the largest run that fits this 13.8 GB machine (~4.2 GB budget):
  241k lines at 2.64 GB peak. The honest extrapolation at ~9 KB marginal
  bytes/line says a full-featured 10M-line synthetic world would not fit 8 GB
  on any hardware; the remaining lever — spilling the ledger to parquet
  between stages and scanning with per-stage projections — changes the
  FullRunResult contract that the web export, API and report all consume, and
  is logged as the next scale step rather than half-done.
- **Demo web bundle is not byte-stable across separate sessions** (pre-existing,
  documented in Phase 35): iterative solver outputs (PPML/IRLS) can move in
  the last ulp with environment conditions and one value sits on a
  10-significant-digit rounding boundary. Engine CSV/parquet exports are
  immune (solver values round before export). Possible later hardening:
  round solver outputs before bundling too.
- **Five npm moderate advisories accepted in writing** (docs/security.md):
  the tailwind-3 chain (fix is a Tailwind 4 migration; the parser runs only
  at build time on our own CSS) and react-router v6 variants fixed only in
  v7. Both migrations are deliberate major-version projects, deferred.
- **Docker is validated in CI only** — this machine has no Docker. The
  compose stack is smoke-tested on every push by `.github/workflows/docker.yml`.
- **Per-action volume verdicts are weak by design** (~0.26 log points of
  90-day Poisson noise per pair). Reported as information on every outcome;
  the pooled calibration is the supported read.

## Decisions waiting on the owner

1. **Review and merge `run5`.** Nothing is pushed; the public demo deploys
   from `main` and is untouched until you merge.
2. **Scale roadmap.** Approve (or defer) the disk-spill pipeline as the next
   scale step — it is the one remaining lever for the 10M-line target and
   touches the FullRunResult contract.
3. **Deferred major migrations**: Tailwind 4 and react-router 7 (clears the
   five accepted npm moderates), noted in docs/security.md.
4. **Replay feed in Docker** is off by default; one env flag
   (`MARGIN_API_REPLAY: "1"`) turns it on if the demo should run live.
5. **Action-center identity** is a configured user name, not authentication —
   acceptable for local/demo use; real login belongs to a hosting decision.

## Verification like CI (Phase 47)

Per the Run 5 spec, before the final commit the branch was verified from a
fresh clone in a temporary folder (never relying on files that exist only on
this machine): `uv sync --frozen --all-groups` and the full pytest suite, then
the Pages workflow steps — `margin-engine export-web --demo`, `npm ci`,
production build, and `CI=true SKIP_SNAPSHOTS=1 npx playwright test`. Results
are recorded in the Phase 47 build-log entry with exit codes.
