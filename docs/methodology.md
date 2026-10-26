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

### Charges, taxes and cost pools (Run 6)

Every billed charge code can declare its semantics in the mapping (economic
owner, direction, pool, amount basis, and that order-level charges are never
embedded in net amounts): pass-through charges stay revenue with an honest
label, while charges declared as taxes, deposits or third-party collections
NEVER enter product revenue — their total stays a visible liability and the
billed-charges reconciliation still ties to the source table exactly. A
line-level tax column declares whether it is embedded in net (carved out of
product revenue) or billed separately (never added); the tax-free
net_product amount is what every realized-price comparison uses, and the
collected tax is a labeled liability column. The realized unit price is net
product / quantity: allocated billed charges never enter the price index
(tested), and neither does an embedded tax.

Cost pools are counted once. Every activity rate belongs to a named pool
(order service, line service, outbound freight, special handling) and every
actual expense source covers its own pool. When an actual freight-expense log
exists, the per-delivery activity rate — which proxies outbound freight — is
switched OFF with a stated note, so delivery cost is never estimated on top
of the actual; any other rate assigned to an actually-covered pool fails the
run. The cost-pool audit table (cost_pools export) shows which pool is
covered by an actual source, which by an estimate, and which rate was
switched off.

Returns carry a disposition (resaleable, damaged, obsolete, not received,
exchange — normalized by the mapping) that controls whether the return
recovers product cost: resaleable and exchange returns put the product back;
damaged, obsolete and not-received returns recover nothing, so the loss
stays in cost of goods sold. Without a disposition column, cost recovery is
assumed and stated — the historical behavior, never a silent change.

### Economic event taxonomy (Run 6)

Every adjustment is also classified by its ECONOMICS, independently of raw ERP
labels (the mapping normalizes source codes to canonical reasons; the taxonomy
classifies the canonical data): customer price concession, customer refund or
return, customer service payment, non-price customer claim, supplier rebate or
allowance, supplier quality claim, vendor payment for a distinct service,
vendor selling-cost reimbursement, freight pass-through, tax or deposit or
third-party collection, and other adjustment (stated, never silently
absorbed). Each event carries counterparty, direction, source document and
reference, original invoice link, program, earn and settlement periods,
eligible basis, estimate-vs-actual status and a reversal/true-up link. The
events table is an analytical classification: the ledger keeps every source
dollar and reconciliation is untouched by it.

Duplicate representations: a credit memo that appears both embedded as a
negative line on its original invoice and as a standalone credit document is
flagged on both rows, counted ONCE in event totals (the embedded posting
wins), and the excluded amount is reported. The ledger keeps both rows'
dollars because source totals must still reconcile - the engine surfaces the
double posting loudly instead of silently dropping a source row.

Vendor consideration follows ASC 705-20: it reduces acquisition cost by
default. A program the data declares as payment for a distinct good or
service, or as reimbursement of identifiable selling costs, is an exception:
it never nets into product cost and is reported as its own economic event.
A vendor rebate roll-forward separates rebates EARNED on receipts from
rebates RECOGNIZED against the cost of goods sold; the balance belongs to
inventory still on hand and never hits current-period line cost. Without an
inventory-receipts table the roll-forward is reported as not testable.

Multiple simultaneous rebate programs: a customer may hold several programs
with overlapping eligibility. Each program accrues on its own tier table over
the same cumulative basis and the line carries the SUM - no dollar is counted
twice and no ledger row is duplicated. Settlements match per customer, year
and program; a year where only some programs settled uses actuals for those
and accruals for the rest (partially settled). A settlement naming a program
the customer does not hold stays a visible residual. A customer-year whose
eligible basis is negative (returns exceed purchases) accrues nothing - the
fail-closed outcome is reported, never a negative rebate invented.

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
x month. The economic floors are exact integer math. Units are stated for
every symbol: C, K, F and all floors are cents per normalized unit; m, m_c
and d are dimensionless rates; price indexes (current_index, floor_index,
band percentiles) are dimensionless ratios to the effective list price.

    pocket gross floor          = C / (1 - m)                        [cents/unit]
    invoice gross floor         = (C / (1 - m) + F) / (1 - d)        [cents/unit]
    pocket contribution floor   = (C + K) / (1 - m_c)                [cents/unit]
    invoice contribution floor  = ((C + K) / (1 - m_c) + F) / (1 - d)[cents/unit]

with C the latest per-unit landed cost net of vendor rebates [cents/unit],
m the minimum pocket GROSS margin ((pocket revenue - product cost) / pocket
revenue), m_c the optional minimum CONTRIBUTION margin (also subtracts
cost-to-serve), K the pair's estimated per-unit cost-to-serve from config
activity rates [cents/unit, labeled estimated], d the customer's
rebate-program rate plus realized freight-concession rate (both reduce pocket
revenue), F fixed per-unit deductions [cents/unit]. The BINDING invoice floor
is the larger of the two invoice floors; guidance reports both and names
which binds (`binding_floor_basis`).

Floor-index comparisons divide the binding invoice floor [cents/unit] by the
effective list price [cents/unit], giving a dimensionless floor_index in the
same space as the realized price index — so index-space and dollar-space
comparisons agree exactly whenever the list price is valid (tested). The
comparison is made only when the pair's list is present, positive and not
stale (older than `max_list_age_months` at the pair's last sale); otherwise
the pair keeps its dollar floors and `list_provenance` says why no index
comparison was made. A price index computed against the item-median fallback
is never compared to a floor index: the two live in different reference
spaces.

Guardrails (Run 6, Phase 67). Bands are PAIR-WEIGHTED (one median per
customer x item x month — the stated weighting) and report the independence
behind them: distinct customers, pairs, months and list regimes per band. A
band needs the configured minimum of independent observations AND distinct
customers — one customer's history is not a market reference. Targets and
starts are UNAVAILABLE, with the reason named, when the segment is thin, the
floor sits above the corridor, or the pair's list is stale (a target in index
space against a list no longer in force is a number nobody can act on; the
dollar floors are kept either way). An item trading in several normalized
units without an equivalence conversion never enters a band and its pairs say
why. The contribution floor stays OPT-IN (off by default) and can be scoped
to configured item roles and channels; when it binds, the K assumption that
makes it bind is disclosed next to it (order-type scoping would need an
order-level floor, which does not exist at pair grain — a stated limitation).
Explicitly authorized below-floor roles (loss leaders, strategic key-value
items, end-of-life clearances) keep their visible below-floor status, and fix
actions generate BLOCKED with the authorization shown — configuration is the
only override path. Optimized prices snap to the configured ERP price
increment, with the floor rounded up to the increment first so a snap can
never breach it. Guidance disclosures per pair: list provenance, contract
eligibility, the unit basis of every comparison, and the as-of date of the
CURRENT unit cost guidance prices against (historical margins use dated
historical costs; the two are labeled, never mixed).

Statuses: in_band, below_reference, below_floor, above_reference,
insufficient_pricing_headroom (floor above band P75 — flagged, never papered
over with a fake target), market_reference_below_floor (the segment's P50
sits below the binding floor, so the historical reference cannot be the
target), insufficient_data. Precedence: insufficient_data >
insufficient_pricing_headroom > below_floor (the pair's own breach, the most
actionable per-pair fact) > market_reference_below_floor > band statuses.
Targets and starts are max(P50/P75, floor) and labeled
`provisional_historical`: historical benchmarks are not optimal prices.

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

### Estimator specification

Each estimator is specified in full so its estimand is never conflated with
another's (Run 6, 60.3). Shared setup: the panel is customer x item x month
within one commercial segment; contract pairs and unreliable one-off outliers
are excluded as described above; no weights are applied (each panel row counts
once); rows dropped by each estimator are counted and reported.

**Naive FE-OLS.**
Dependent variable: log quantity. Sample: positive-quantity rows only (zeros
drop with the log). Endogenous regressor: log price, NOT instrumented — price
and demand shocks are simultaneously determined, which is the point of
reporting it. Fixed effects: pair and month (item-role x month when roles are
known). Clustering: the configured level (default two-way month x customer).
Named estimand: the ASSOCIATION of price and conditional quantity among buying
pairs — documents endogeneity bias, never used for decisions.

**FE-Poisson (PPML).**
Dependent variable: quantity in levels, including true zeros. Sample: all
active-assortment rows; zero rows carry the pair's last observed price
(flagged carried-forward; a positive-rows-only variant is estimated and the
gap joins the robustness criterion). Regressor: log price, not instrumented.
Fixed effects: pair and month; separated observations (groups whose outcome
is all zero) are dropped by the estimator and counted. Named estimand: the
descriptive semi-elasticity of TOTAL expected quantity — extensive and
intensive margin together — among active-assortment pairs, still subject to
price endogeneity.

**FE-2SLS.**
Dependent variable: log quantity. Sample: positive-quantity rows only —
dropping zeros means the estimand is CONDITIONAL quantity among pairs that
kept buying, not total volume; this is stated wherever the estimate is used.
Endogenous regressor: log price. Instrument matrix: supplier cost index and
category tariff rate (two instruments, overidentified). Fixed effects: pair
and month (item-role x month when roles are known), applied by within
transformation to outcome, price and instruments; the optional market-price
control is partialled out of all of them (Frisch-Waugh). Clustering: the
configured level for F, the Anderson-Rubin set and the leave-one-cluster-out
jackknife interval. Dropped observations: zeros, rows with missing price or
instruments, and singleton groups absorbed by the fixed effects — each
counted. Named estimand: the own-price elasticity of conditional quantity
among buying pairs, identified from instrument-induced price variation.

**IV-PPML control function (added by Phase 72).**
Dependent variable: quantity in levels including zeros. First stage: log
price on the instruments and fixed effects; its residual enters the PPML
equation as a control function. Inference: bootstrap or jackknife (the
control function is generated). Named estimand: the own-price elasticity of
TOTAL expected quantity, including whether the customer buys at all — the
estimand the opportunity optimizer needs. Specification details land with
Phase 72 and are recorded here.

### Estimand and the competitor-price path (Run 6)

The estimand is the **unilateral own-price response holding observed
competitor-price conditions constant** — the quantity a pricing decision
needs. Both shipped instruments are industry-wide cost shocks, so they move
competitor prices too; without those conditions held constant, the IV
identifies a response to a market-wide cost shock, which UNDERSTATES
unilateral price sensitivity (a customer facing an industry-wide increase has
nowhere cheaper to go). Three estimands are therefore reported and labeled:
unilateral own-price response (the gate passed), market-wide cost-shock
response (IV ran, the path is uncontrolled — with the bias direction stated),
and descriptive association (no instrument behind it).

`causal_elasticity_supported` now additionally requires the competitor-price
path to be ADDRESSED: either an adequate public market price index, or every
instrument's competitor-price path documented as absent with a written
argument. Otherwise the status caps at `iv_estimated_elasticity` with the
reason, and downstream the two-sided expected-change range is withheld —
an interval for a market-wide estimand is not a confidence interval for the
unilateral move being priced, and its bias direction flatters price increases.
There is deliberately no configuration switch that disables this gate.

Control adequacy is structural and relevance-based, never a threshold on
collinearity. A faithful market-price control MUST correlate with
industry-wide cost instruments — that correlation is the confound it exists to
absorb — so rejecting a control for being correlated with them would reward a
column of noise and punish an informative series. The checks, each reported
with its statistic: freshness (trailing lag in periods, evaluated first on
the covered periods); coverage (share of the segment's estimation rows);
absorption (distinct control values per time fixed-effect cell — a national
monthly series is removed exactly by month effects, a structural fact tested
structurally); grain (the control must be at least as fine as the CHANNEL's
grain, which config declares and argues: customers compare sellers of the
same item class, so a category-level index is the right grain even for a
supplier-level instrument); relevance (the share of within-pair price
movement the index explains, with the right sign — a series unrelated to
prices is not evidence of a competitor-price condition); and restatement
(only near-exact collinearity is refused, because controlling for a restated
instrument removes identification rather than a confound).

The first stage is reported both before and after controlling, because
controlling costs identification. Measured on the demo world: holding the
category market price constant removes nearly all of the CATEGORY tariff
instrument's variation (its own first stage falls to F below 6) while the
SUPPLIER-specific cost shock keeps F in the high hundreds. That is the
economics, not an artifact: a category tariff *is* a category market-price
mover, whereas a supplier-specific cost shock moves this distributor's cost
without moving the category market price — which is exactly what identifies a
unilateral move. Two consequences, both made explicit rather than hidden:
the overidentification test runs only over instruments that still identify
price (fewer than two leaves the model just-identified and the test reported
as not evaluated, which the spec's own criterion contemplates), and the
robustness spread is computed only over IDENTIFIED alternative
specifications, with the excluded ones named together with their first-stage
F. A specification whose own first stage is weak is not evidence that the
headline is unstable — it is an unidentified specification. When NO
alternative is identified the spread is reported as unknown and the top
status is refused, rather than collapsing to a reassuring zero.

Every criterion is recorded machine-readably (`causal_status_reasons`: order,
gate, pass / not met / not evaluated, and a plain-English detail), exported
long-form as `sensitivity_gates`, and shown in the UI as a checklist whose
outcome is carried by a glyph and words, never colour alone. A check the data
could not support is reported as NOT EVALUATED and never counted as a pass.

The gate does not discharge shared-exclusion risk in general: once a control
is present, a violation that both instruments share through some other
channel still passes every criterion with a non-rejected overidentification
test. That limitation is stated here and in the status tooltip, which is why
`causal_elasticity_supported` means "the prespecified diagnostics and
documented assumptions passed for this estimand and scope" — not that the
instruments are valid, nor that the result transfers elsewhere.

Diagnostics: cluster-robust first-stage F; identifying-variation coverage;
Sargan-style overidentification J with two instruments; robustness spread
across IV-with-each-instrument-alone and the joint estimate; Anderson-Rubin
confidence set by grid test inversion (weak-IV robust). The primary 95%
interval is a full leave-one-cluster-out jackknife (first stage re-estimated
per left-out cluster) with t(G-1) critical values — calibrated at small
cluster counts where plug-in sandwiches under-cover — verified by
50-simulation coverage studies; the AR set is reported alongside and takes
over under weak identification. Clustering level is configurable: by month,
by supplier x month, or two-way month x customer (Cameron-Gelbach-Miller
combination of per-dimension jackknives, V = V_month + V_customer -
V_intersection), the robust choice when customer demand shocks persist over
time. The shipped default is chosen by the persistent-demand coverage study
recorded in validation.json.

Each segment also records its identifying price support: the 5th-95th
percentile range of instrument-predicted within log-price movement
[dimensionless, log price units]. Downstream volume-response math refuses
proposed price moves outside that range (`outside_price_support`):
extrapolating a demand curve beyond the variation the instruments ever
induced would be a claim the data cannot back.

Carried-forward-price robustness: zero-quantity rows carry the pair's last
observed price, which is an assumption, so PPML runs twice — on the full
panel including zeros and on positive rows only — and the gap between the two
joins the robustness criterion. The share of carried-forward rows is reported
per segment.

Optional competitor control: when a `market_price_index` reference table is
mapped (category x month), its log joins the models as a control. Documented
limitation: industry-wide cost shocks also move competitor prices, so without
the control (and partly even with it) estimates likely UNDERSTATE own-price
sensitivity — a customer facing an industry-wide increase has nowhere cheaper
to go, so the measured volume response is flatter than the response to a
unilateral move. The overidentification test cannot detect this: both
instruments violate exclusion in the same direction.

Evidence statuses:

- `insufficient_data` — too few observations or no within-pair price variation.
- `descriptive_elasticity` — estimable associations; also the cap when the
  first stage is weak (F below threshold) or IV support is thin (configurable
  minimum observations/pairs for IV claims).
- `iv_estimated_elasticity` — IV ran with adequate first stage, but at least
  one causal criterion failed (overidentification rejected, robustness spread
  too wide, CI too wide).
- `causal_elasticity_supported` — every criterion passed AND a documented
  exclusion rationale exists in config for every instrument. Since Run 4 the
  criteria also require the Anderson-Rubin set to be bounded and no wider
  than `ar_max_width_ratio` (default 1.5x) times the primary interval: when
  weak-IV-robust evidence disagrees with the plug-in interval, the top status
  is not awarded.

Partial pooling: segment x item-category estimates shrink toward the parent
segment with precision weights (method-of-moments between-variance); raw and
shrunk estimates, effective sample size and uncertainty are all reported.

Known limitation (by design of the test itself): the overidentification test
detects an invalid instrument only when the two instruments disagree.
Exclusion restrictions remain economic assumptions that no statistical test
fully proves.

## 8. Opportunity and scenarios

Three levels, never summed. Units: prices and gaps are cents per normalized
unit on the INVOICE basis (index x effective list); pocket conversion applies
the pair's percentage post-invoice deductions d.

1. Observed leakage: reconciled dollars over the baseline window [cents].
2. Gross price opportunity: eligible quantity x positive invoice gap x
   (1 - d), at unchanged volume [pocket cents] — a raised invoice price also
   raises the rebates and concessions that scale with it, so the pocket gain
   is (1 - d) of the invoice gap. A conservative variant raises below-P25
   pairs to P25 only, reported alongside (never added to the main level).
3. Expected change with a volume response, reported on TWO bases that are
   never conflated or summed (Run 6, Phase 65): the expected POCKET GROSS
   PROFIT change uses unit economics c(P) = P (1 - d) - F - C [cents/unit];
   the expected CONTRIBUTION change also subtracts the pair's estimated
   per-unit cost-to-serve K (c(P) = P (1 - d) - F - C - K, labeled estimated
   because K comes from configured activity rates) and is UNAVAILABLE for a
   pair without a supported K — never borrowed from the gross profit basis.
   Both are c(P1) Q(P1) - c(P0) Q(P0), produced only where the elasticity
   status is causal AND the proposed move lies inside the segment's
   identifying price support; at `iv_estimated_elasticity` a labeled
   sensitivity range is shown; below that, fixed-volume numbers only.
   Because a price increase sheds volume and its service cost with it, the
   contribution change EXCEEDS the gross profit change by K x (Q0 - Q1) —
   high-service pairs can rank differently under the two bases, which is the
   point of keeping both.

**Deductions with explicit denominators** (Run 6, Phase 66 / Finding 8):
every post-invoice adjustment has a declared FORM and BASE. Flat program rates
and realized freight-concession rates are both percentages of the SAME base —
net invoiced value — and apply independently, so they sum into d exactly;
rates on different bases are never summed. Fixed per-unit deductions F are
cents per unit. TIERED programs are retrospective formulas — the rebate is
rate(cumulative basis) x cumulative basis — so every opportunity figure for a
tiered customer is RECOMPUTED at the candidate revenue (crossing a tier
re-rates the whole basis, exactly as a settlement would); a tiered customer
whose tier table is missing keeps the flat (1 - d) figure labeled a
fixed-rate approximation, never silently. Each record carries its deduction
treatment, and the per-customer coverage table (deduction_coverage export)
reports how much of d is observed history, contract-derived rates, or a
recomputed tiered formula. Fixtures prove the reported impact equals a
baseline-vs-scenario ledger recomputed through the full pipeline, to the
cent, for flat, fixed, tiered and mixed arrangements.

**Per-unit cost-to-serve K** (Run 6, Phase 65 / Finding 8): the pair's
activity cost over the covered baseline divided by its units, decomposed into
a VARIABLE component (per-line and per-unit handling rates over observed
lines and units) and an ORDER component (per-order and per-delivery
allocations). Both derive from the pool-resolved effective rates, so a rate
switched off by the cost-pool overlap check never re-enters through K, and
the floor's K and the optimizer's K are the same number — the derivation is
stated here precisely so neither can be circular with the other.

Pairs without a known unit cost are excluded from cost-dependent levels and
labeled `cost_unavailable` — missing cost is never treated as zero.
Eligibility excludes contract-protected pairs and insufficient-data guidance.
Every record carries baseline window, action, constraints, quantity basis,
price assumptions, evidence status and a calculation trace.

Target optimization maximizes the CONFIGURED objective (Run 6, Phase 65) and
every output names it. Under `pocket_gross_profit` the objective is
(P (1 - d) - F - C) Q(P), with the log-log interior optimum
P* = (C + F) / (1 - d) x beta / (1 + beta) [cents/unit, invoice basis]. Under
`contribution` the objective also subtracts K, and the MARGINAL K follows the
declared order-cost response: with a fixed ordering cadence
(`fixed_order_frequency`, the default) order quantities scale with volume, so
the per-order component's TOTAL is volume-constant and drops out of marginal
economics; with `fixed_order_size` order counts scale with volume and the
whole K is marginal. The contribution optimum is found NUMERICALLY (coarse
grid plus cent-resolution refine — exact for this smooth, unimodal objective
and still correct if unit economics gain further quantity dependence); a pair
without a supported K keeps the provisional target. Either way the optimum is
applied only at causal support inside the identifying price range, clipped to
the floor and corridor; inelastic segments get the corridor top, labeled. All
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
close on won deals) informs the start price. Win-aware optimization (Run 6,
Phase 65) translates the quote index into a monetary price and maximizes the
POCKET objective times the win probability — (P (1 - d) - F - C) x P_win(P),
or with per-unit cost-to-serve K also subtracted under the contribution
objective (refused, not guessed, when no K is supported) — with deal size
held at baseline: ONE volume-response source
per record, enforced by the function signature. An elasticity estimated from
realized transactions (PPML on the panel including zeros) already contains
the extensive margin the win curve models — lost deals ARE the zeros — so
multiplying Q(P) by P_win(P) would count the same response twice. The
demand-elasticity path is the separate opportunity optimizer.

## 9. GAAP alignment (ASC mapping)

The engine is a MANAGEMENT view. Where it touches figures an accountant would
recognize, the mapping is:

- **Net invoiced revenue** (the headline KPI) is invoice revenue after
  returns and credit memos. It is close to, but not, GAAP revenue: under ASC
  606 variable consideration (expected rebates, discounts and refunds) is
  estimated and netted at recognition, while this view nets rebates at the
  pocket stage and recognizes returns when they occur rather than through a
  refund-liability estimate (ASC 606-10-55, right of return).
- **Pocket revenue** approximates GAAP net sales: invoice revenue less
  customer rebates and freight concessions — consideration payable to a
  customer (ASC 606-10-32-25) reduces revenue, exactly as rebates do here.
  The difference is timing: accrued estimates during the period are labeled
  `estimated` and trued up at settlement.
- **Product cost** follows the declared cost basis (standard / average /
  FIFO / LIFO / landed) over receipt layers (ASC 330 inventory); vendor
  rebates reduce acquisition cost (ASC 705-20 consideration received from a
  vendor), never customer-facing revenue.
- **Freight**: billed freight is revenue; actual outbound freight expense is
  cost-to-serve BELOW gross profit. GAAP permits a shipping-and-handling
  policy election (ASC 606-10-25-18B treats shipping as fulfillment); many
  filers carry freight in cost of sales. The management view keeps it below
  gross profit so product margin and service cost stay separately visible —
  a stated presentation difference, not a measurement one.
- **Returns** are recognized when they occur and linked to originals. GAAP
  books a refund liability and a right-of-return asset at sale; the
  difference is visible in the monthly GL variance check when one is run.

### Monthly GL variance check (Run 6)

The GL comparison is a CONTROL, not a proof. Net sales and cost of goods sold
reconcile separately per period; every month-measure shows engine total, GL
total, signed and absolute variance, the variance percentage (no percentage is
shown when the GL amount is near zero - the cents tolerance judges those
months, so a tiny denominator can never manufacture a reassuring or alarming
percent), the tolerance used, the fiscal-period mapping and the account
mapping. Unresolved reconciling items (periods missing from either side, the
known-cost coverage share on COGS) are listed, and a deterministic trace
sample follows individual lines from the source document into the monthly
bridge. Statuses are distinct and honest: GL exact, GL within tolerance, GL
variances outside tolerance, and not testable when no GL file is supplied -
absence of the control is stated, never an implied pass. The engine's
always-on source-total reconciliation (exact to the cent) is a separate,
stronger property this check builds on; the headline reads "every source
dollar reconciled or explained" with the visible residuals and coverage next
to it, because allocated completeness beyond that would be a claim the data
does not back.

## 10. Reproducibility

Every run writes a manifest: config and mapping hashes, input file
fingerprints (sha256), software versions, cleaning decisions, parameters and
stage timings. The DuckDB store keeps both views and the audit trail keyed by
source line key.

## 11. Pricing-law considerations

Orientation for reviewers, not legal advice. Pricing decisions at scale touch
several bodies of law, and the engine's job is to surface the facts a counsel
or compliance reviewer would ask for — never to conclude.

**Price discrimination (Robinson-Patman Act).** Selling commodities of like
grade and quality to competing buyers at different prices can be unlawful when
it may harm competition. Recognized defenses include cost justification (the
price difference reflects differences in the cost of serving the two buyers)
and meeting competition. The engine raises a *review flag* when two customers
who are likely competitors of each other — by default, same industry and same
region, configurable in `compliance.robinson_patman` — carry recommended
prices for the same item more than a configured threshold apart. Beside every
flag it shows the cost-to-serve difference between the two customers for that
item, because that is the first number a cost-justification review needs. The
flag marks a question, not a violation: whether the customers actually compete,
whether the goods are of like grade and quality, and whether a defense applies
are judgments the engine cannot make. Actions on flagged pairs require a
written justification at approval (Phase 38 guardrail).

**Algorithmic pricing and information sharing (Sherman Act section 1).**
Recent enforcement and litigation treat pricing algorithms fed with
competitors' non-public data as potential facilitators of price coordination,
even without an explicit agreement. For that reason the engine accepts a
market/competitor price index as an elasticity control **only from public
sources**: the mapping must declare `public_source: true` for the
market_price_index table, and anything else switches the control off with a
stated note (`compliance.market_index_public_source_required`). Public indexes
(e.g. producer price indexes) carry no such concern.

**What the engine never does.** It never ingests competitor transaction data,
never recommends coordinating prices with competitors, and never silences a
compliance flag: a flagged action can be approved, but only with a recorded
written justification.

## 12. Action center

### Action generation

Engine findings become proposed actions deterministically (content-hash ids;
dates anchored to the data's last invoice date, so the same world always
generates the same actions). Five types, each with a plain-English title and
rationale whose every number comes from the engine's outputs, and an expected
dollar impact at the *right* opportunity level for its type. Impact bases are
never summed across types — the three opportunity levels answer different
questions (section 8), and an action list that added them would be claiming a
total that does not exist.

| Action type | Expected-impact basis |
|---|---|
| Price change | Level 3 (expected contribution change) when the volume response is causal-supported **and** the move sits inside the segment's identifying price support; otherwise level 2 (gross price opportunity) at fixed volume, labeled |
| Below-floor fix | Raise to the binding floor at fixed volume, in pocket dollars (conservative by construction); a pair with no baseline volume states that instead of inventing a number |
| Cost pass-through | Recovered cost at the configured pass-through level (default 50%, never full) from the same scenario grid the report uses |
| Service-policy review | Contribution at stake after activity costs, labeled estimated (activity rates are config estimates) |
| Rebate / discount review | Level-1 observed leakage in excess of the customer's segment-median leakage rate, with ratio and absolute-dollar gates |

Guardrails are enforced at generation, not documented as advice: an unblocked
proposal below the binding floor raises an error (never clamped silently);
contract-protected pairs generate *blocked* actions with the reason shown and
can never become price changes; the Robinson-Patman review flag (section 11)
travels with the action; priority defaults by impact and evidence, with a
non-causal evidence status demoting one level.

### Stale actions, safe transitions and export recovery (Run 6)

A proposal is a claim about the world at generation time, so every action
stores the fingerprints it rests on — the source files' content hashes, the
config hash, and a model fingerprint derived from the segment elasticity
statuses and estimates — plus an expiry date. At approval and at export the
engine REVALIDATES the stored key values against its current outputs: current
price, binding floor, list provenance, contract protection, guidance
sufficiency, compliance flags, scope duplication and expiry. Every move is
shown field by field with both versions written to an append-only
revalidation audit table; a move inside the configured threshold is reported
and allowed, a move beyond it or a hard change (contract protection appears,
guidance goes insufficient, the proposed price now sits below the floor, the
pair vanishes, another active price takes the scope) refuses the approval and
sends the action back for re-review. A check the available outputs cannot
support is NAMED as not revalidated — never counted as a pass. Export applies
the same test and HOLDS OUT stale rows with their reason rather than shipping
them on old evidence.

Concurrency: every transition can carry the caller's expected event sequence
number. The check and the insert run inside one database transaction with the
count re-read under it, so two simultaneous transitions cannot both land — the
loser gets a 409 naming the sequence it expected and the one it found, and
writes nothing.

Export recovery: an export journals its intent (export id, path, the action
ids it covers) BEFORE writing the file and marks the journal committed only
after the status transitions land. A crash in between leaves an open journal
row, which the next export (or an explicit recovery pass) completes for
exactly those actions — skipping any that have since moved past approved, so
recovery never re-exports a superseded or implemented price. Recovery is
idempotent: running it twice changes nothing.

### Scope keys, overlap and portfolio totals (Run 6)

Every action carries its scope metadata: a scope key (for price actions, the
uniqueness unit customer x item x unit x effective date), baseline period,
intervention, affected set, scenario link and overlap group. ONE active
proposed price may exist per scope key: approving a duplicate is refused with
the competing action named, and the price export refuses a file that would
carry two prices for one scope (defense in depth). Cross-scope overlaps - a
pass-through scenario and pair price actions on the same items - share an
overlap group, and the portfolio table carries each group's JOINT estimate
under one baseline: price actions keep their single-action impacts (deduped
by scope key) while the pass-through recovery is recomputed with the priced
pairs excluded from its base. The naive sum is shown beside it only to make
the double counting visible; neither figure is ever added to the single-action
lists, and the three quantities (single-action impact, scenario impact,
portfolio estimate) stay distinct in the UI, the CSV exports and the API.
Portfolio groups are computed at engine-run time over the generated action
set; manual proposals join the duplicate guard through their scope keys but
not the precomputed groups (stated in the API payload).

### Review workflow (maker-checker)

Lifecycle: proposed → under review → approved | rejected → exported →
implemented → measured, with rejected actions reworkable back to proposed.
Every transition is an event in an append-only log (who, when, from-status,
to-status, note); the current status is **derived by replaying the log**,
never edited in place, and a stored log is re-validated as a legal walk, which
catches in-place tampering.

Roles: analyst (proposes, edits, submits, exports, marks implemented),
approver (approves or rejects), viewer (reads only); a user may hold several.
Separation of duties: an approver cannot approve an action they proposed or
submitted. Approving a compliance-flagged action requires a written
justification, recorded with the event. Edits are whitelisted (owner, due
date, title, rationale, proposed price), allowed only before approval, refused
on blocked actions, floor-checked, and every edit lands in an append-only
audit table. "Measured" belongs to the results tracker, never to a user.
Identity in local mode is a configured user name the client sends with each
request — explicitly **not** authentication; the policy layer is the same once
real login exists.

### ERP price export

Approved price actions (price change, below-floor fix — the types that carry a
proposed price) become an ERP-importable price file: generic CSV by default,
with header names, date format, decimal separator, delimiter and price unit
configurable per deployment. Every row carries the effective date, previous
price, new price, UOM, approval id and action id. The export is idempotent:
the file always contains every approved-or-already-exported price action,
deterministically formatted, so re-exporting rewrites identical bytes; only
actions still in "approved" receive the exported transition, so no event is
logged twice. The diff summary shown before export reports expected impact by
basis — never one summed total. A round-trip test re-imports the exported file
through the engine's own ingest and matches every price to the cent.

### Results tracking and its limits

After an action is marked implemented, the tracker measures it over a
configurable window (default 90 days on each side of the implementation date):

- **Price realization** = (P_after − P_before) / (P_new − P_before) on the
  pair's invoiced prices: 1.0 fully realized, 0 no move, negative eroded
  further. This is the strongest per-action signal, because it is directly
  observable.
- **Volume against prediction.** The treated pair's volume ratio
  (after/before) is compared to the median ratio of untreated same-segment
  pairs with sales in both windows — a difference-in-differences design, so a
  market-wide trend never reads as a price effect. The prediction is the
  `predicted_volume_ratio` stored on the action **at generation time**
  ((P1/P0)^elasticity for causal-supported volume responses, 1.0 for
  fixed-volume actions), so the engine is graded on what it claimed when the
  action was proposed, not on a number chosen after the fact.
- **Margin outcome**: pocket margin and contribution, before vs after.

Every gate fails closed with its reason on the outcome row: window not yet
elapsed, too few after-window sales (a pair that stopped buying is reported as
exactly that), too few comparison pairs. Calibration — prediction error by
evidence bucket (causal-supported / IV-estimated / fixed-volume) — appears
only once a configured minimum of actions is measured; below it the table says
"not enough implemented actions yet" instead of any number.

**Limits, stated plainly.**

- A single pair's 90-day window carries roughly 0.2–0.3 log points of Poisson
  noise, which usually exceeds the planted effect of a realistic price change.
  Per-action volume verdicts are therefore weak evidence; the *pooled*
  comparison is the meaningful read. On a synthetic world with 30 planted +8%
  interventions the pooled recovery error is 0.039 log points while the
  per-pair median absolute error is 0.26 — both reported in
  `docs/validation.json`.
- The difference-in-differences comparison assumes untreated same-segment
  pairs carry the treated pair's counterfactual trend (parallel trends). A
  shock specific to the treated customer biases the measured effect, in either
  direction.
- Only implemented actions are measured, and which proposals get implemented
  is a human choice — measured outcomes are a selected sample, not a random
  one, so calibration describes the actions that were taken, not all actions
  the engine could propose.
