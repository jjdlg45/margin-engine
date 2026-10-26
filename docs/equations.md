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
- `d` — percentage post-invoice deductions as a rate: the customer's rebate
  program rate plus the customer's realized freight-concession rate. Held in
  basis points internally. Assumption: these deductions scale proportionally
  with invoice value (a fixed-rate approximation; Phase 66 adds explicit
  denominators and recomputation for tiered arrangements).
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

Level 3 — expected change with a volume response [pocket cents]:

    unit economics today:  c(P) = P x (1 - d) - F - C
    level3 = c(P1) x Q(P1) - c(P0) x Q(P0)

Produced only where the elasticity status is causal-supported AND the
proposed log-price move lies inside the segment's identifying price support;
at instrumented-only evidence a labeled sensitivity range from the elasticity
confidence interval is shown instead; below that, fixed-volume numbers only.
Pairs without a known cost are excluded (missing cost is never zero).

Pending change (Phase 65): `c(P) = P x (1 - d) - F - C` is a pocket GROSS
PROFIT basis and will be reported under that name; the expected CONTRIBUTION
change will subtract per-unit cost-to-serve K: `c(P) = P x (1 - d) - F - C - K`.

## Target optimization

For log-log demand and the pocket objective `(P x (1 - d) - F - C) x Q(P)`,
the first-order condition gives the interior optimum on the invoice basis:

    P* = (C + F) / (1 - d) x beta / (1 + beta)     for beta < -1

`(C + F) / (1 - d)` is the pocket-aware marginal cost of an invoice dollar.
P* is clipped to [binding floor, band P75]. For beta >= -1 the unconstrained
optimum is unbounded, so the corridor top is used and labeled. Applied only
at causal support inside the identifying price range; everything else keeps
the provisional historical target (max(P50, floor), start max(P75, floor)).

Assumptions: d and F constant in quantity and price; C constant over the
move. Pending change (Phase 65): the objective becomes configurable (pocket
gross profit or contribution, subtracting K), and the optimizer re-optimizes
numerically when d, F or K depend on quantity.

## Win curve (quotes)

Per segment, a logistic model on the quote price index `x = quote / list at
the quote date`:

    P_win(x) = 1 / (1 + exp(-(a + b x)))

Win-aware optimal price (grid search over the index, deal size at baseline):

    P*_win = argmax over P of (P - C) x P_win(P / list)

clipped to [floor, upper corridor]. ONE volume-response source per record:
the win curve is never multiplied with a demand elasticity (lost deals are
the zeros PPML already models). Negotiation start: `start = target / (1 -
negotiation gap)`, with the gap measured on won deals as `1 - close / quote`.

Pending change (Phase 65): the objective translates the index to a monetary
price and applies d and F (and K under the contribution objective):
`(P x (1 - d) - F - C) x P_win`, today it is `(P - C) x P_win` (gross basis,
no post-invoice deductions).

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
(1.0 for fixed-volume actions). Gates fail closed: window not elapsed, too
few after-window sales, or too few control pairs produce a reason, not a
number. Calibration reports the median absolute volume error and median
realization by evidence bucket, only once the configured minimum of measured
actions exists.

Pending change (Phase 75): treated pairs with zero after-window purchases
will count as a volume ratio of 0 with a stopped-buying flag (today they fail
the after-sales gate), controls will not require purchases in both windows,
and pooled calibration moves to a staggered difference-in-differences
estimator; the per-action output is named "control-adjusted volume change",
not a difference-in-differences estimate.

## Margin and leakage rates

    pocket gross margin = (pocket revenue - product cost) / pocket revenue
    contribution margin = (pocket revenue - product cost - cost to serve)
                          / pocket revenue
    leakage rate        = observed leakage / net invoiced revenue

No margin is reported when its denominator is zero or negative; the dollars
are still reported.
