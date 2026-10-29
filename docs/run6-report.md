# Run 6 report — independent review remediation

An independent methodology review of commit `d641fc8` raised 24 findings.
This is what changed for each one, what it cost in headline numbers, what is
still open, and what needs an owner's decision.

**Every number here is a measurement.** Current-state figures come from the
committed artifacts (`web/public/data/bundle.json`, `docs/validation.json`)
and can be re-read at any time; before/after figures are quoted from the
phase reports in [`docs/build-log.md`](build-log.md), which recorded them
when the change was made. Where a figure has moved since the phase that
recorded it — later phases changed the demo world — both values appear, and
the current one is marked as such. Nothing in this document was typed from
memory.

## The short version

The review's through-line was that the engine's labels promised more than its
methods delivered. Most of the remediation is therefore **subtraction**:
numbers that used to appear now refuse to, and the refusals are the product.

Three results are worth stating before the detail, because each one makes the
engine look worse on its own demo and is correct:

- **Both win curves dropped to `insufficient_precision`** once standard errors
  stopped assuming that 676 quotes from 24 customers were 676 independent
  observations (Finding 21).
- **One commercial segment disappeared** when features that embedded the price
  being explained were removed — and bootstrap stability rose from 0.737 to
  0.884 (Finding 11).
- **The spill-to-Parquet scale path does not lower peak memory** at the scales
  measured, and ships off by default saying so (Finding 23).

## Findings 1–24

### Finding 1 — Contribution conflated with gross profit (Phase 65)

Expected-change was one metric doing two jobs. It is now two, never summed:
**expected pocket-gross-profit change** (`P(1−d) − F − C`) and **expected
contribution change** (also `− K`, the per-unit cost to serve, labelled
estimated when it comes from config rates). The optimizer objective is
configurable and every output names which one it used. The win-curve
objective translates the quote index to money and applies `d` and `F` before
multiplying by win probability.

*Before/after (build-log Phase 65):* expected contribution change
7,799,686 → 7,843,222 cents; level-3 impact on a contribution basis
3,796,446 → 3,863,155 cents; the fixed-volume level-2 bucket
20,164,261 → 20,110,146 cents.

*Tests:* high-price/high-service vs low-price/low-service customers ranking
differently by objective; a quote optimum that differs between gross margin
and pocket contribution; numeric-vs-closed-form agreement.

### Finding 2 — Competitor-price path not in the causal gate (Phase 70)

`causal_elasticity_supported` now requires an adequate market/competitor price
control, or an instrument documented as having no competitor-price path.
Otherwise the status caps at `iv_estimated_elasticity` with the reason
"competitor-price path not controlled". A machine-readable
`causal_status_reasons` list carries every gate passed or failed.

*Measured (validation.json, "Competitor-price path gate"):* 13 of 30
segment-seeds reach causal support; 17 of 30 are just-identified after
controlling, because a category tariff instrument is itself a category
market-price mover.

### Finding 3 — IV estimand excluded zero sales (Phase 72)

FE-2SLS on log quantity silently dropped every pair-month with no purchase —
the extensive margin a price increase acts on. An **IV-PPML control-function**
estimator now includes zeros, with bootstrap inference. The optimizer uses the
total-quantity estimate where available; where only the positive-quantity
2SLS estimate passes, it is labelled "conditional quantity elasticity (buying
customers only)" and is **not** used alone to forecast total volume.

*Tests:* `tests/test_total_quantity.py` (20) and `tests/test_zero_row_prices.py`.

### Finding 4 — Clustering below the instrument-assignment level (Phases 71, 73)

Supplier, supplier × month, category and category × month join the cluster
options, with an **effective** cluster count (inverse Herfindahl of per-cluster
instrument variation) rather than a raw count. Below the threshold the interval
is resampled by wild cluster bootstrap or withdrawn.

**The default did not change, and that is the finding.** Measured coverage
under persistent demand (validation.json): month × customer 278/300 = 92.7%
(shipped), month-only 253/300 = 84.3%, supplier × month 244/300 = 81.3%.
Assignment-level clustering is right in principle and cannot cover at twelve
suppliers, because what breaks coverage is the customer dimension. The
build-log Phase 73 entry records the A/B that settled it, including an
equal-tailed bootstrap that measured *worse* than doing nothing and was
replaced by the symmetric form.

*Tests:* `tests/test_clustering.py` (28).

### Finding 5 — Economic event taxonomy and rebate lifecycle (Phases 61, 62)

Every adjustment is classified by its economics rather than its ERP label,
carrying counterparty, direction, source document, earn and settlement
periods, eligible basis, estimate-vs-actual status and reversal links. Vendor
consideration follows ASC 705-20 concepts, with rebates on inventory still on
hand held in a **vendor rebate roll-forward** rather than hitting current-period
line cost. The rebate subledger enforces its balance invariant as an exception,
not a warning: opening + estimates + revisions − settlements − reversals =
closing.

*Tests:* `tests/test_events.py`, `tests/test_rebate_subledger.py`,
`tests/test_tiered.py`, including a credit present in two exports counted
once, a voided-and-replaced invoice, a clawback and a negative net rebate basis.

### Finding 6 — Freight, charges and overlapping cost pools (Phase 63)

Every charge declares economic owner, direction, pool, basis and whether it is
already embedded in net amount. Each real expense pool carries a `cost_pool_id`
and is assigned to exactly one of actual cost-to-serve or activity estimate —
an overlap **fails the run**. Taxes, deposits and third-party collections are
out of product revenue by default. Returns gained a disposition controlling
whether product cost is recovered.

*Test (the review's "already correct, add test"):*
`test_billed_freight_never_enters_price_index`.

### Finding 7 — Books tie-out named as more than it was (Phase 64)

Renamed **"Monthly GL variance check"**, with engine total, GL total, signed
and absolute variance, variance %, tolerance, fiscal-period and account
mappings, and unresolved reconciling items — net sales and COGS separately.
Four distinct statuses including `not testable (GL not supplied)`, and guards
on zero, negative and tiny denominators. The headline changed from "Every
dollar accounted for." to **"Every source dollar reconciled or explained."**

### Finding 8 — Deduction denominators and per-unit K (Phases 65, 66)

Each adjustment declares its base — % of invoice value, % of eligible net
purchases, per unit, per order, per shipment, tiered/cumulative, or unknown —
with an explicit denominator and sequencing rule. Rates are summed only when
they share a base; tiered arrangements are **recomputed** at the candidate
price and predicted quantity, otherwise the result is labelled "fixed-rate
approximation". Known deduction coverage is reported.

*Before/after (build-log Phase 66):* gross price opportunity
40,450,015 → 39,629,675 cents once tiered customers' rebate growth was
recomputed rather than approximated.

### Findings 9, 10 — Floors and bands (Phase 67)

The contribution floor stays **opt-in** (default off); when enabled it shows
the K assumptions that make it bind. Bands state their weighting and count
**independent** units, reporting distinct customers, pairs, months and list
regimes. List provenance, customer eligibility and UOM basis are disclosed;
historical margin uses dated historical cost and current guidance uses current
cost, both labelled. Target and start are **unavailable** — not
historical-only defaults — when the floor is above the corridor, the list is
stale, or the segment is thin.

### Finding 11 — Segmentation leakage (Phase 76)

The review named `revenue_abc`; the audit found two more of the same bug.

- **ABC class**: was cut on the same period's revenue (price × quantity). Now
  cut on a window ending *before* each line's own period, basis configurable.
  No history in the window is `unrated`, not C.
- **Order-size band**: was cut on order *net value* — the identical leak in a
  feature nobody had looked at. Now cut on quantity.
- **Target encoding**: was fit on the rows it encoded. Now cross-fitted
  out-of-fold.
- **The guard** checked feature *names*; `revenue_abc` contains none of the
  forbidden words. It now reads declared lineage and refuses anything that
  declares it embeds the realized price — or declares nothing at all.

*Before/after (build-log Phase 76, demo world):* segments 3 → 2; bootstrap
median ARI 0.737 → **0.884**; recommendations flipping across resamples
41.7% → 31.4%; gross price opportunity $396,296.75 → $413,735.22.
*Current (bundle.json):* 2 segments, ARI 0.8840696765, gross opportunity
41,373,522 cents.

A segment disappeared because the split between the two upper-index segments
was being drawn with features carrying the prices it separated.

*New:* a **time-based holdout** — fit on the first 18 months, scored on the
last 6: 0.0621 mean absolute error against a 0.1145 single-median baseline
(current bundle.json), with the order-size band worth 0.0003 out of time.

*Tests:* `tests/test_segment_leakage.py` (25), including a planted world where
a shortcut predicts price as well as the truth but cuts across it: declared
honestly the guard refuses it; declared as price-free the tree takes it and
recovery falls by more than 0.2 ARI.

### Finding 12 — Validation statistics (Phase 78)

`world_bootstrap` resamples **worlds**, not observations: segments inside one
world share its shocks, instruments and prices. On a fixture with real
between-world spread the world-level interval is **more than three times
wider** than treating observations as independent — a test, not a claim. Gated
elasticity rows report 90% intervals on the median and P90 against a stated
precision goal; coverage reports an interval on the coverage rate itself. Ten
**held-out seeds** (1009–1061, outside the coverage study's range) are added
and reported separately.

*Tests:* `tests/test_validation_statistics.py` (9).

### Finding 13 — Competitor path and weak-IV interaction (Phases 70, 71)

Covered by Findings 2 and 4. The J test and Anderson-Rubin interpretations are
permanent tooltips: J cannot detect a violation both instruments share; AR is
robust to weak instruments but cannot fix a wrong exclusion story. Unbounded or
disconnected AR sets are reported as such, never compressed into a symmetric
interval. Placebo and lead tests were added for instruments.

*Measured (validation.json):* lead-test false rejections 0/30 segment-seeds;
effective F 361–1985 against the published 23.109 bar.

*Tests:* `tests/test_weak_iv.py` (33).

### Finding 14 — Price-support guard (Phase 74)

Relabelled an **identification-support rule**, computed on residualized
within-pair movements after fixed effects and controls. Support is now
**regions**, not a single range: a segment whose instruments moved price around
−6% and +4% supports neither, where the old rule waved a flat price through
because 0% sits between them. Enough observations near the proposed move are
required, with the boundary and distance reported.

*Current (bundle.json):* 193 opportunity records sized at unchanged volume for
being outside price support.

*Tests:* `tests/test_price_support.py` (15).

### Finding 15 — Results tracker (Phase 75)

- **Renamed** "control-adjusted volume change"; the difference-in-differences
  name is reserved for the pooled estimator that states its assumptions.
- **Stop-buying is an outcome, not missing data.** A treated pair with no
  after-window purchases was recorded as `insufficient_evidence` and dropped —
  the most consequential outcome a price increase can have, systematically
  removed from the measurement of price increases. It now counts as a volume
  ratio of 0, on both sides of the comparison.
- A **pooled event study** uses never-treated and not-yet-treated controls, so
  an already-treated pair is never someone else's counterfactual. Pre-trends
  are reported with the size of pre-trend the data could have detected.
- Clean controls exclude pairs carrying their own action at any date, sharing a
  campaign, or selling a substitute.

*Current (bundle.json):* the pooled estimate reports `estimated`, ATT +10.7%,
`no_pretrend_detected`. The Phase 75 entry recorded +23.2% with a detected
+19.8% pre-trend on the demo as it stood then; the demo's segments and actions
have changed since (Findings 11, 21), so both are recorded rather than one
quietly replacing the other.

*Tests:* `tests/test_tracker_design.py` (17), with exit-gate worlds for
non-parallel trends, selected treatment, stop-buying and staggered treatment.

### Finding 16 — Action overlap and portfolio totals (Phase 68)

Every action carries `scope_key`, baseline period, intervention, affected set,
scenario id and `overlap_group`. One active proposed price per customer × item
× UOM × effective date; duplicates are merged, superseded or blocked at
approval. A category pass-through and a pair price change on the same items are
combined in a **portfolio scenario** under a common baseline, never summed.

*Tests:* `tests/test_action_overlap.py`.

### Finding 17 — Robinson-Patman review flag (Phase 79)

- "Same industry and region" became a candidate **screen**, overridden in both
  directions by an optional reviewed `customer_competitors` table. Every pair
  states its basis.
- A customer with a missing attribute used to drop out of the comparison
  silently. It is now counted and reported as unscreened: **not screened is not
  the same as not competing.**
- Each flag carries the comparable item, role and UOM, the dates each side's
  prices and costs come from, current and recommended prices on an invoice
  **and** a net-of-deductions basis, volumes, channel, contract position,
  terms, cost to serve with its method, a data-coverage field, and any
  meeting-competition record or reviewer decision. Absent evidence reads
  absent, not clear.

*Measured (current bundle.json):* 27 flagged pair-item combinations, 50
screened customers, 0 unscreened, all screened candidates. The first flagged
pair is 7.64% apart on invoice price and 3.57% apart after terms (build-log
Phase 79) — a review comparing invoice prices alone would read the wrong two
numbers.

*Tests:* `tests/test_compliance_screen.py` (15), including one that walks every
note, column name and value and fails on "violation", "compliant", "illegal",
"unlawful" or "guaranteed".

### Finding 18 — Market data provenance and pricing-law wording (Phases 80, 81)

`public_source: true` — an assertion anyone could write — was replaced by
recorded provenance: provider, URL, retrieval timestamp, licence, cadence,
coverage, aggregation level and **series kind**. The kind decides the role: a
cost index may instrument and is refused as the competitor-price control; a
selling-price index is the reverse. An approved-publisher allowlist is
available and off by default, with unlisted providers reported rather than
silently accepted.

The pricing-law section now opens by saying the guardrails are **not safe
harbors**: nothing in it makes a price lawful, and a clean run is not evidence
in a dispute. The DOJ's proposed consent decree with Pinnacle (filed
4 September 2026, RealPage matter, M.D.N.C.) is cited for the distinction that
matters — it does **not** outlaw pricing software; it draws the line at
algorithms fed with rivals' data, which is the line the provenance rule is
built around.

### Findings 19, 20 — Stale actions, safe transitions, production identity (Phases 69, 82)

Proposals store input, config and model fingerprints and the key values used;
approval and export revalidate current price, list age, floor, contract
protection, exclusions, duplicates and compliance flags, show the differences
and require re-review past thresholds. Transitions carry an expected event
sequence number — conflicts get 409 and no state change — and export recovers
idempotently from a crash between writing the file and updating status.

Phase 82 added a **production mode**. `X-User` was a header the client filled
in, which meant separation of duties was decided by the party it constrains. In
production that header is *rejected*, identity is a server-issued bearer token
against PBKDF2-hashed local accounts or an OIDC verifier, and every
authorisation reads an **immutable user id**. Uploads gained owners (the id was
a content hash and was never a secret), a retention window and owner-only
modes; an append-only audit log records who/what/when and refuses fields that
look like secrets. Production refuses to start when nobody could log in, or
when encrypted storage has not been declared.

*Tests:* `tests/test_stale_actions.py` and `tests/test_production_mode.py` (32),
including server-side self-approval rejection walked through the real HTTP
lifecycle with tokens.

### Finding 21 — Win curves beyond a standard-error gate (Phase 77)

- **Only decided quotes are evidence.** `outcome == "W"` was a win and
  *everything else* a loss, so an open quote counted as the customer saying no
  and a withdrawn one as a price rejection. Codes are classified against
  configured lists; open, withdrawn and expired are excluded with reasons, and
  an unrecognised code is excluded with the code named, never read as a loss.
- **One decision is one observation**: revisions collapse to the latest version
  of their opportunity where a quote id exists.
- **Quotes are not independent draws**: standard errors cluster at the
  opportunity, customer or rep level, with the number of groups reported.
- **Held-out calibration** (slope, intercept, Brier, c-statistic) downgrades a
  precise curve of the wrong shape; too few held-out quotes reads "not
  testable".

*Measured (current bundle.json):* both demo segments are now
`insufficient_precision` — segment 6 on 676 quotes from **24** customers,
segment 24 on 1,140 from **49**. The 10% relative-SE bar is unchanged; the
standard error it reads is now the right one. 1,816 of 1,865 quote rows are
modeled. The 200-customer validation world still clears the bar, so the
section 8 target holds where the data supports it.

*Tests:* `tests/test_win_curves.py` (17), including a measured demonstration
that the old rule biases the coefficient **toward zero** — understating how
much price matters, the direction that flatters a price increase.

### Finding 22 — Validation rows without scope (Phase 78)

`docs/validation-meta.yaml` carries, per measured check, the worlds or clusters
behind it, the method, what it establishes and what it does not. Rendering
refuses a check with no scope entry, a scope entry for a check nobody measures,
or any blank field. The README keeps a compact table with a "worlds or
clusters" column; [`docs/validation.md`](validation.md) carries every row in
full. Several entries say "one world", because that is one draw.

*Tests:* `tests/test_validation_table.py` (14).

### Finding 23 — Scale (Phase 83)

Built in full: `scale.spill_ledger` / `--spill`, per-stage projections, stage
classification written down, per-stage peak memory sampled onto every manifest,
and parity proven byte-for-byte on the sample and on every third
scenario-matrix world.

**The benchmark then said it does not help.** Three worlds, each run in its own
process (build-log Phase 83):

| World | Lines | In-memory peak | Spill peak |
|---|---|---|---|
| 150 × 250 | 70,514 | 1.52 GB | 1.58 GB |
| 300 × 500 | 227,388 | 2.79 GB | 3.05 GB |
| 120 × 1,250 | 226,587 | 2.79 GB | 3.07 GB |

The peak never lived in the analytic middle the spill empties; it lives at the
export step, where a re-sorted, string-cast copy of the ledger must exist for
byte-identity, beside the resident observations frames the ledger spill does
not touch. It ships **off by default** as parity-proven infrastructure rather
than a saving it has not earned.

### Finding 24 — Overclaiming (Phase 78)

- Online Retail II is described as the UCI description does — real UK
  online-retail (giftware) transactions, many customers wholesalers — and the
  README states the case study tests **ingestion and descriptive analytics
  only**.
- The hybrid PPI world is labelled **hybrid-synthetic evidence**.
- Determinism is worded to what was tested: byte-identical across runs,
  separate processes and thread counts on the tested platform; identity across
  operating systems and BLAS builds is untested. `_rs` rounds 73
  iterative-solver fields to 6 significant digits so the claim holds rather
  than holds by luck.
- Performance claims apply to the measured configuration only.

## Phase 85 edge cases

Beyond the 24 findings, the review listed edge cases. All are now covered by
`tests/test_edge_cases.py` (17 tests):

- **Spreadsheet formulas with no cached result** fail actionably instead of
  reading as blanks — "honest arithmetic on dishonest input" is the failure
  mode avoided.
- **Hidden rows** are kept (a hidden row is still data, and a filtered export
  can hide the lines under review) and **counted**, with the run stating how
  many were hidden and that they are included in every total.
- **Date serials** are named as serials rather than reported only as "does not
  match the declared format", because the fix is in the workbook.
- **Mixed separators** were already covered per table
  (`test_per_table_delimiter_override`).
- **Mapping changes between runs**: the config fingerprint now hashes the rules
  **and** the mapping, because a mapping change redefines what the numbers mean.
- **An action spanning multiple UOMs** is blocked — one proposed price would be
  ambiguous — and one spanning **multiple ship-tos** says so in its affected set.
- **Parent/child contracts**: protection recorded at an account or parent now
  reaches child lines, which were previously treated as freely priceable —
  eligible for increases on exactly the customers a contract forbids repricing.
- **FX analytical vs book translation**: a converted ledger states that the
  engine converts at each line's invoice-period rate, so a GL translated at
  closing or average rates differs by policy, not by a missing dollar.

## The acceptance matrix

`tests/test_acceptance_matrix.py` implements the reviewer's 22 families plus
the 5 edge-case families above as a checked registry: each family names the
modules and load-bearing tests that discharge it, and a test resolves every
reference against the code. A renamed or deleted test fails the matrix instead
of quietly thinning it.

## What is still open

**The 10M-line scale target remains a KNOWN GAP** — the only failing row in
`docs/validation.json` (34 of 35 pass). It cannot be verified on the 13.8 GB
build machine. Phase 83 narrowed the next steps to two levers, both owner
decisions:

1. **Streaming the ledger export.** Blocked by byte-identity between the eager
   and streaming Parquet writers. "Byte-identical exports" is a published
   claim, so relaxing it is not mine to decide.
2. **Spilling the observations frames** the same way — they stay resident
   through the whole tail in both modes.

**`docs/validation.json` has not been regenerated since Phase 71**
(2026-10-26T20:23Z). Phase 78 widened the study to fifteen worlds per gated
elasticity target with world-level bootstrap intervals and held-out seeds, and
that study does not fit this machine — `.github/workflows/validation.yml` runs
it on a runner by manual dispatch and uploads the refreshed documents. Until it
runs, the validation table carries the Phase 71 measurements and the new
per-world and uncertainty fields are absent. The scope document
(`docs/validation-meta.yaml`) and `docs/validation.md` are current.

**Two duplicate rows** for the competitor-price path gate sit in
`validation.json` with identical measurements. A measured artifact is not
edited by hand; they clear at the next regeneration.

**The Run-1 performance row** is measured by the CI benchmark job, not locally.
Phase 76's cross-fitting added measured cost to the segmentation stage: the
stage A/B median went 2.25 s → 3.06 s (+23%) at 210k lines, down from 5.98 s
(+166%) for the naive per-fold refit, and inside the end-to-end run the stage
moved 3.07 s → 3.54 s (+0.47 s), of which 0.40 s is the time-based holdout.
The CI benchmark job is the gate.

## Decisions for the owner

1. **Relax byte-identity for the ledger export?** It is the blocker on the
   largest remaining memory lever (Finding 23).
2. **Run the validation workflow.** The enlarged study's numbers are the ones
   the README table should carry; dispatch
   `.github/workflows/validation.yml` and commit the artifact.
3. **Turn on the contribution floor?** It stays opt-in by policy decision
   (Finding 9), and enabling it changes which floor binds.
4. **Draw up a market-data allowlist?** `market_index_require_allowlist` is off
   because most deployments have no approved-source list on day one.
5. **Production mode before any shared hosting.** Local mode is explicitly not
   authentication; `docs/deploy-api.md` says what an operator must supply,
   including the OIDC verifier the engine deliberately does not ship.
6. **Model fingerprints changed in Phase 84.** Proposals persisted under the
   old derivation read as config-changed once against a fresh run; the
   stale-action revalidation path exists for exactly that and shows the
   difference.

## A process note

Two failures of my own are recorded in the build log rather than quietly fixed,
because they bear on how much the rest of this document should be trusted:

- I wrote an **unmeasured figure** ("2,268 s") into two source comments and a
  draft phase report, in the grammar of a measurement. It was caught and
  removed before that phase committed, but the correction was never written
  down until a later audit. That audit also found the README validation table
  contradicting `validation.json` on six rows — including a 37.0 s pipeline
  time against a measured 119.0 s — under a paragraph claiming "No number is
  hand-typed". The table is now generated and a test fails if it drifts.
- Three different **unrun checks** were reported as passes across two days: a
  memory-limited test described in prose at the bottom of a report, a property
  suite the batched runner silently skipped because it globbed one directory
  level, and a phase committed with "58 of 59 files passing". Each is now
  structurally prevented — a CI-run marker, a runner that cross-checks against
  pytest's own collection, and the rule that a phase commits only when every
  check that runs locally passes.
