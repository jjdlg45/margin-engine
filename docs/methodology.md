# Methodology

This document explains what each stage of the engine computes, the statistical
models used, and the exact meaning of every evidence status. For validation
results, see the README table; for the chronological build record, see
`docs/build-log.md`.

## 1. Ingest and mapping semantics

A mapping YAML declares, for every column, both the source name and the
business meaning: currency and sign convention for every amount; cost basis
(standard / average / FIFO / landed), inclusion flags (inbound freight, duty,
vendor rebates) and unit basis for cost; date formats; transaction-code
mapping; reversal handling. Any calculation whose required semantic is missing
fails with an error naming the field, the missing semantic and the calculation
— the engine never guesses what a column means.

Money is parsed digit-exact from strings into 64-bit integer cents (scale 1e-4
for unit prices and costs). No monetary value that must reconcile ever passes
through floating point. Rounding is half-even. Values with more than four
decimals are refused rather than truncated.

### Multi-currency (Run 2)

List prices and costs are declared in the reporting currency; revenue amounts
on foreign-customer lines arrive in the line's currency (the mapping declares
`currency: line_currency`) and convert at ingest with exact integer arithmetic
(half_even(cents x rate_ppm / 1e6)) at the invoice month's rate (policy
`invoice_date`). Settlement amounts convert at their period's last-month rate;
order charges at the order month's rate. Originals are kept in `*_src` columns
with the applied rate per line; reconciliation conserves each currency's
source total exactly. A missing rate is an actionable error — the engine never
guesses an exchange rate.

### Supported input formats

Tabular invoice-line exports only: CSV (configurable delimiter, encoding with
BOM handling, currency symbols, minus or parentheses negatives, whitespace
trimming) or .xlsx via a pure-Python reader (sheet declared in the mapping;
numeric cells rendered digit-preserving, date cells rendered ISO). Both routes
join the same validation path; money parses digit-exactly from strings under
the declared options, and any physical-format failure reports the likely cause
and the mapping option to set. PDF or scanned invoices are out of scope; data
must already be line-level.

## 2. Transaction classification and the two views

Classification follows the spec's table: returns and credits are classified
(credits by reason into price vs. cost), duplicates are removed only on
verified identity (same stable source key AND identical content; same key with
different content is flagged as a key collision and kept), look-alike rows are
flagged and kept, missing cost is distinguished from genuine zero cost, and
zero-quantity rows are excluded from unit-price math and flagged.

Two views are produced. The **sales observations** view contains clean sale
lines for behavioral estimation. The **financial ledger** keeps every row.
Excluding a row from observations never removes its dollars from the ledger.

## 3. Waterfall and allocations

Seven stages per line, all integer cents: gross list value (effective-dated
list x normalized quantity; list price is never manufactured from the invoice
price), invoice revenue (net + allocated billed charges), pocket revenue
(minus line-accrued rebates, allocated settlement rebates, freight concessions
and attributed price credits), product cost (declared basis built up to landed
cost, vendor rebates netted against base purchase cost), pocket gross profit,
cost to serve (allocated freight expense + non-price claims), contribution.

Allocations are exact by the largest-remainder (Hamilton) method: each line's
exact share is floored to the cent and the remaining cents go one at a time to
the largest fractional remainders (ties: larger absolute weight, then source
line key), so allocated amounts always sum to the source amount and no line is
ever a full cent away from its exact share. Settlement rebates allocate over
sales and returns with signed weights (returns carry negative rebate shares).
A credit dollar is counted exactly once: credits attributed to their original
invoice contribute zero on the credit row. When an amount cannot be
attributed, it remains a visible residual in the reconciliation report.

Return economics: returns are classified by reason (customer_error,
distributor_error, defective — mapped from the source reason code; unmapped
codes become `unspecified`). Restocking fees and return freight billed to the
customer are recovery revenue in pocket; return handling (per line + per unit)
and return freight expense are cost-to-serve. Amounts present in the export are
`actual`; otherwise the reason-policy table and config defaults produce
`estimated` values, always labeled.

Reconciliation checks (row conservation, net-amount conservation, allocation
conservation for charges / settlements / freight incl. the return freight log,
stage identities) are exact to the cent and raise on failure.

## 4. Dispersion and outliers

Prices normalize to an index (realized unit price / effective-dated list;
item-median fallback is labeled). Peer groups are item x UOM x contract regime
x quarter, falling back to item family below 30 observations. Outliers use the
modified robust z-score 0.6745 x (x - median) / MAD at 3.5 within peer groups,
with two refinements, each labeled in `outlier_method`:

- MAD = 0 cohorts fall back to flagging deviations > 10% from the modal price.
- A second stage flags one-off deviations from the line's own customer-item
  median (same statistic, same threshold, within the same peer group), because
  peer groups legitimately mix customer segments wide enough to hide a deep
  one-off concession that is glaring against the pair's own history.

Two mutually exclusive flags: `data_quality_anomaly` (>= 3x or <= 1/3 of the
peer median — entry-error magnitude) and `commercial_pricing_outlier`
(everything else). Original records are never modified or removed.

## 5. Commercial segments and margin drivers

Commercial segments are a decision tree on the normalized price index using
context features only (industry, size band, region, channel, contract type,
item family, item role, order-size band, revenue ABC). A leakage guard rejects
any feature whose name contains price/discount/cost/rebate/margin terms.

Method details, each load-bearing:

- The tree is fit at the customer x item pair level (median index per pair).
  Line-level fitting would pseudo-replicate high-volume pairs.
- Categorical features enter through shrunken target encoding, the standard
  CART treatment that lets one split group similar categories; the published
  rules are pure context-set memberships.
- CHAID-style consolidation merges cells whose mean index differs by less than
  a configurable commercial-distinctness threshold (default 0.015): cells
  reached by different branches but priced the same are one segment.
- Leaf minimums (max(500 lines, 0.5%) plus >= 20 distinct customers and >= 10
  distinct items) are enforced on the final partition by nearest-median
  merging.
- Contract-priced lines are excluded: their prices are governed by contracts.

Stability is measured by partition agreement (adjusted Rand index) across
bootstrap refits, never by comparing raw leaf ids, and the share of
provisional targets that would change across resamples is reported.

The margin-driver tree (pocket margin %, context + discount / rebate / cost
bands, depth 4) is a separate, explanatory output and is never used to justify
price increases.

## 6. Price guidance

Reference bands are P25/P50/P75 of the normalized index within commercial
segments; the default observation unit is one median price per customer x item
x month. The economic floor is exact integer math:

    pocket floor  = C / (1 - m)
    invoice floor = (C / (1 - m) + F) / (1 - d)

with C the latest per-unit landed cost net of vendor rebates, m the minimum
pocket margin, d the customer's rebate-program rate plus the customer's
realized freight-concession rate (both reduce pocket revenue), F fixed
per-unit deductions. Statuses: in_band, below_reference, below_floor, above_reference,
insufficient_pricing_headroom (floor above band P75 — flagged, never papered
over with a fake target), insufficient_data. Targets and starts are
max(P50/P75, floor) and labeled `provisional_historical`: historical
benchmarks are not optimal prices.

## 7. Price sensitivity

Panel: customer x item x month. Zero rows exist only where the pair is in
active assortment (bought within the configured lookback) AND the item was
available (proxy: sold to anyone that month — stockouts are not directly
observable; the proxy is labeled). Zero rows carry the pair's last observed
price, flagged `price_carried_forward`.

Models per segment (contract pairs excluded; flagged outlier lines excluded):

- Naive FE-OLS (pair + month fixed effects) — reported to document
  endogeneity bias, never used for decisions.
- FE-Poisson (PPML) on the panel including true zeros.
- FE-2SLS with two instruments: supplier cost index and category tariff rate,
  both observed in reference tables.

Time fixed effects run at item-role x month when roles are known: key value
items are broadly co-carried and co-move within a month, and absorbing that
common component keeps clustered inference honest. Flagged one-off commercial
outliers are excluded from estimation only when the pair has enough history
(>= 6 observations) for the one-off judgment to be reliable — on sparse pairs
a genuine shock-driven price move is indistinguishable from a one-off, and
excluding those points provably biases sparse-segment elasticities.

Diagnostics: cluster-robust first-stage F; identifying-variation coverage;
Sargan-style overidentification J with two instruments; robustness spread
across IV-with-each-instrument-alone and the joint estimate; Anderson-Rubin
confidence set by grid test inversion (weak-IV robust). The primary 95%
interval is a full leave-one-cluster-out jackknife (first stage re-estimated
per left-out cluster) with t(G-1) critical values — calibrated at small
cluster counts where plug-in sandwiches under-cover — verified by a
50-simulation coverage study; the AR set is reported alongside and takes over
under weak identification. Clustering level is configurable; the default
configuration clusters by month, inside which both the instruments' variation
and the customer-month demand-shock correlation live.

Evidence statuses:

- `insufficient_data` — too few observations or no within-pair price variation.
- `descriptive_elasticity` — estimable associations; also the cap when the
  first stage is weak (F below threshold) or IV support is thin (configurable
  minimum observations/pairs for IV claims).
- `iv_estimated_elasticity` — IV ran with adequate first stage, but at least
  one causal criterion failed (overidentification rejected, robustness spread
  too wide, CI too wide).
- `causal_elasticity_supported` — every criterion passed AND a documented
  exclusion rationale exists in config for every instrument.

Partial pooling: segment x item-category estimates shrink toward the parent
segment with precision weights (method-of-moments between-variance); raw and
shrunk estimates, effective sample size and uncertainty are all reported.

Known limitation (by design of the test itself): the overidentification test
detects an invalid instrument only when the two instruments disagree.
Exclusion restrictions remain economic assumptions that no statistical test
fully proves.

## 8. Opportunity and scenarios

Three levels, never summed: observed leakage (reconciled dollars over the
baseline window), gross price opportunity (eligible quantity x positive gap to
the proposed pocket price at unchanged volume), and expected contribution
change ((P1-C1)Q(P1) - (P0-C0)Q(P0)) — the latter only where the elasticity
status is causal; at `iv_estimated_elasticity` a labeled sensitivity range is
shown; below that, fixed-volume numbers only. Eligibility excludes
contract-protected pairs and insufficient-data guidance. Every record carries
baseline window, action, constraints, quantity basis, price assumptions,
evidence status and a calculation trace.

Target optimization (P* = argmax (P - C_var) Q(P), the log-log interior
optimum C_var x beta/(1+beta)) applies only at causal support, clipped to the
floor and corridor; inelastic segments get the corridor top, labeled. All
other targets remain provisional historical.

Cost/tariff scenarios state scope (item / supplier / category), magnitude and
effective date; pass-through strategies 0% / 50% / 100% and segment-weighted
are shown side by side; full pass-through is never assumed; contract-protected
lines are excluded and their exposure reported. Combined scenarios are modeled
jointly — overlapping single-scenario numbers are never added.

### Quote win/loss modeling (Run 2)

Per commercial segment, a logistic regression of quote outcome on the quote
price index (quoted price over the effective-dated list at the quote date).
Statuses are fail-closed:

- `insufficient_data` — fewer quotes than `sensitivity.min_quotes_per_segment`
  (default 400), no price variation, or separable outcomes.
- `insufficient_precision` — the fit converged but its own relative standard
  error (se / |coefficient|) exceeds `sensitivity.win_curve_max_rel_se`
  (default 0.10), so the data does not support the accuracy an estimated curve
  claims. Coefficient, SE and CI stay reported; the status keeps the curve out
  of accuracy gating and win-aware optimization.
- `estimated_win_curve` — fitted within the precision gate.

The precision threshold is calibrated so that estimated curves recover planted
coefficients within +/-15 percent per segment: across validation worlds, every
segment at or under the threshold recovered within 7 percent, while segments
above it erred by up to 28 percent. The gate is roughly a 1.5-sigma criterion
against the +/-15 percent bound, so it is probabilistic rather than a
guarantee — stated here rather than hidden. The negotiation gap (quote to
close on won deals) informs the start price, and win-aware optimization
maximizes (P - C) x Q(P) x P_win(P) only where the segment's elasticity is
causal-supported and the win curve is estimated.

## 9. Reproducibility

Every run writes a manifest: config and mapping hashes, input file
fingerprints (sha256), software versions, cleaning decisions, parameters and
stage timings. The DuckDB store keeps both views and the audit trail keyed by
source line key.
