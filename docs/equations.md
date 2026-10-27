# Equation sheet

Every floor, opportunity level, optimizer, win-curve and tracker formula the
engine computes, with its price basis and its deduction and cost assumptions
stated. Symbols are shared across the sheet:

- `P` — invoice-basis unit price [cents per normalized unit]. Invoice basis
  means the product's net price after on-invoice discounts, excluding billed
  charges. Index-space prices are `P / list` (dimensionless) against the
  effective-dated list in force.
- `C` — per-unit product cost on the declared basis, built up to landed cost
  and net of vendor rebates [cents per unit].
- `K` — estimated per-unit cost-to-serve from configured activity rates
  [cents per unit, labeled estimated].
- `d` — percentage post-invoice deductions as a rate: the customer's flat
  rebate program rates plus the realized freight-concession rate. Held in
  basis points internally. Rates sum into d only because they share one base
  (net invoiced value) and apply independently (Phase 66); rates on different
  bases are never summed. TIERED programs never flatten into d: their rebate
  is rate(cumulative basis) x cumulative basis and every candidate-price
  figure recomputes it at the candidate revenue (a tiered customer without a
  tier table keeps the flat figure, labeled a fixed-rate approximation).
- `F` — fixed per-unit post-invoice deductions [cents per unit].
- `m`, `m_c` — minimum pocket gross margin and minimum contribution margin
  (dimensionless rates).
- `Q(P)` — expected quantity at price P from the segment's log-log demand:
  `Q(P1) = Q0 x (P1/P0)^beta` with `beta` the estimated own-price elasticity.

Money is exact integer arithmetic end to end; each formula is computed at
full precision and rounded once at the stated point.

## Economic floors (guidance)

Computed at full precision, rounded once at the end; default rounding is
ceiling (a floor exists to guarantee the margin), half-even by config.

    pocket gross floor         = C / (1 - m)                        [cents/unit]
    invoice gross floor        = (C / (1 - m) + F) / (1 - d)        [cents/unit]
    pocket contribution floor  = (C + K) / (1 - m_c)                [cents/unit]
    invoice contribution floor = ((C + K) / (1 - m_c) + F) / (1 - d)[cents/unit]

The binding invoice floor is the larger of the two invoice floors; guidance
reports both and names which binds. Contribution floors exist only where K is
known; a missing K never silently weakens the floor to the gross one without
the basis column saying so.

Floor-index comparison (dimensionless): `floor_index = binding invoice floor /
effective list price`, made only when the pair's list is present, positive and
not stale; a price index computed against the item-median fallback is never
compared to a floor index (different reference spaces).

## Opportunity levels (never summed across levels)

Level 1 — observed leakage [cents over the baseline window]:

    leakage = on-invoice discounts + accrued rebates
            + allocated settlement rebates + freight concessions
            + attributed price credits

An accounting fact reconciled to source totals; no price change is assumed.

Level 2 — gross price opportunity [pocket cents, unchanged volume]:

    gap_invoice = max(0, P_target - P_current)          [invoice basis]
    level2      = baseline_qty x gap_invoice x (1 - d)  [pocket basis]

The (1 - d) conversion recognizes that a raised invoice price also raises the
rebates and concessions that scale with it. F cancels out of price GAPS (it
is fixed per unit) but not of contribution LEVELS. Conservative variant:
raise below-P25 pairs to P25 only, reported alongside, never added.

Level 3 — expected change with a volume response [pocket cents], on TWO
bases that are never conflated or summed (Phase 65):

    pocket gross profit basis:  c(P) = P x (1 - d) - F - C
    contribution basis:         c(P) = P x (1 - d) - F - C - K
    level3 = c(P1) x Q(P1) - c(P0) x Q(P0)

K is the pair's estimated per-unit cost-to-serve (labeled estimated); a pair
without a supported K gets NO contribution figure, never a borrowed one. The
contribution change exceeds the gross profit change by K x (Q0 - Q1): a price
increase sheds volume and its service cost with it.

Produced only where the elasticity status is causal-supported AND the
proposed log-price move satisfies the identification-support rule below; at
instrumented-only evidence a sensitivity analysis from the elasticity
confidence interval is shown instead - a span of plausible price responses,
not a confidence interval around the dollar impact; below that, fixed-volume
numbers only. Pairs without a known cost are excluded (missing cost is never
zero).


## The identification-support rule

Units: log price, dimensionless. Let `m = log(P1 / P0)` be the proposed move
and `x_i` the instrument-predicted within log-price movement of estimation
row `i`, residualized for the pair and period fixed effects and the market
control.

Outer bounds, at the configured quantiles q_lo and q_hi (5% and 95%):

    lo = quantile(x, q_lo),   hi = quantile(x, q_hi)

Density test at a candidate move `m`, with window `w` and minimum count `n`:

    dense(m)  =  |{ i : |x_i - m| <= w }|  >=  n

Supported regions are the maximal intervals of `[lo, hi]` on which `dense`
holds, evaluated on a grid across the bounds:

    R = { [a_k, b_k] }  with  dense(m) for all m in [a_k, b_k]

The rule passes when `m` lies in some `[a_k, b_k]`. Distance to support,
reported on every record and zero when the rule passes:

    dist(m) = min_k max(a_k - m, m - b_k, 0)

One connected region reduces to the outer-bounds test the rule used before.
Two or more mean the instruments moved price in separate clusters, and a move
between them is refused however comfortably it sits inside `[lo, hi]`.


## Target optimization

For log-log demand and the pocket objective `(P x (1 - d) - F - C) x Q(P)`,
the first-order condition gives the interior optimum on the invoice basis:

    P* = (C + F) / (1 - d) x beta / (1 + beta)     for beta < -1

`(C + F) / (1 - d)` is the pocket-aware marginal cost of an invoice dollar.
P* is clipped to [binding floor, band P75]. For beta >= -1 the unconstrained
optimum is unbounded, so the corridor top is used and labeled. Applied only
at causal support and where the move satisfies the identification-support
rule; everything else keeps the provisional historical target (max(P50,
floor), start max(P75, floor)).

The objective is configurable (Phase 65): pocket gross profit (closed form
above) or contribution, which subtracts the MARGINAL per-unit cost-to-serve.
Under a fixed ordering cadence the per-order component's total is
volume-constant and drops out of marginal economics; under fixed order sizes
it is marginal per unit. The contribution optimum is found numerically
(coarse grid plus cent-resolution refine), which agrees with the closed form
where one exists and stays correct under quantity-dependent unit economics.
Every optimizer output names its objective. Assumptions: d and F constant in
quantity and price; C constant over the move.

## Win curve (quotes)

Per segment, a logistic model on the quote price index `x = quote / list at
the quote date`:

    P_win(x) = 1 / (1 + exp(-(a + b x)))

Win-aware optimal price (grid search over the index, deal size at baseline):

    P*_win = argmax over P of (P x (1 - d) - F - C [- K]) x P_win(P / list)

clipped to [floor, upper corridor]. ONE volume-response source per record:
the win curve is never multiplied with a demand elasticity (lost deals are
the zeros PPML already models). Negotiation start: `start = target / (1 -
negotiation gap)`, with the gap measured on won deals as `1 - close / quote`.

Since Phase 65 the objective translates the index into a monetary price and
applies the pair's post-invoice economics: `(P x (1 - d) - F - C) x P_win`,
with K also subtracted under the contribution objective (refused, not
guessed, when no K is supported).

## Results tracker

Per implemented price action, over a window of W days each side of the
implementation date (default 90):

    realization = (P_after - P_before) / (P_new - P_before)

on the pair's invoiced average prices (net amount / quantity, invoice basis);
1.0 fully realized, 0 no move, negative eroded further.

    treated volume ratio  = qty_after / qty_before
    control volume ratio  = median over untreated same-segment pairs of
                            (qty_after / qty_before)
    control-adjusted      = treated ratio / control ratio
    volume error          = log(control-adjusted) - log(predicted ratio)

with `predicted ratio = (P1/P0)^beta` stored on the action at generation time
(1.0 for fixed-volume actions).

`qty_after = 0` is a legitimate value on both sides: the treated pair's ratio
is then 0 with a stopped-buying flag, and a control that bought before and not
after contributes a 0 to the median. `log(0)` is undefined, so the volume
error for a stopped pair is not produced - the pair stays in every count and
is reported separately, never dropped from the measured set.

Gates fail closed: window not elapsed, fewer than the configured minimum sale
lines BEFORE the change (no baseline), or too few clean control pairs produce
a reason, not a number. A control pair is clean when it carries no price
action of its own at any date, shares no campaign with the treated action, and
sells no item in the treated item's substitution group; the counts excluded by
each test appear on the outcome row.

Calibration reports the median absolute volume error and median realization by
evidence bucket, only once the configured minimum of measured actions exists.

### Pooled event study

    E[qty_it] = exp( alpha_i + gamma_t + sum_{k != -1} theta_k * 1{event time = k} )

fitted by Poisson pseudo-likelihood over a pair x month panel, with pair fixed
effects `alpha_i`, calendar-month fixed effects `gamma_t`, and event time
measured from each pair's own implementation month. Event time -1 is the
omitted base period, so each `theta_k` reads against the month before the
price moved. Standard errors cluster by pair.

The control set for any period is the never-treated pairs plus the not-yet
treated ones: a treated pair contributes rows only within `[-leads, +lags]` of
its own treatment, so an already-treated pair never serves as somebody else's
counterfactual.

    effect at k      = exp(theta_k) - 1
    average effect   = exp(mean of theta_k over k >= 0) - 1
    pre-trend check  = the largest |exp(theta_k) - 1| over k < -1

Poisson rather than a log outcome because the panel contains real zeros. The
average-effect interval treats the post coefficients as independent, which
they are not, so it is optimistic - stated on the output. A pre-trend beyond
the configured tolerance marks the post coefficients as not an effect of the
price change; one inside it is reported together with the size of pre-trend
the data could have detected, because a quiet test with no power is not
evidence.

## Margin and leakage rates

    pocket gross margin = (pocket revenue - product cost) / pocket revenue
    contribution margin = (pocket revenue - product cost - cost to serve)
                          / pocket revenue
    leakage rate        = observed leakage / net invoiced revenue

No margin is reported when its denominator is zero or negative; the dollars
are still reported.
