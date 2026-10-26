# Metric dictionary

Generated from `src/margin_engine/metrics.py` - edit there, then run
`uv run python -m margin_engine.metrics` to regenerate. A test asserts
this file matches the source, so the definitions the UI tooltips and
report labels serve can never drift from this document.

Shared conventions: money is integer cents end to end; d is the pair's
percentage post-invoice deduction rate (customer rebate rate plus realized
freight-concession rate); F is the fixed per-unit post-invoice deduction;
C is the per-unit product cost on the declared basis; K is the estimated
per-unit cost-to-serve. The three opportunity levels are never summed.

## List price

The effective-dated list price in force on the invoice date; never derived from invoice prices.

- **Formula:** list price from the price-list history, matched by item and invoice date
- **Units:** cents per normalized selling unit
- **Denominator:** none (a reference price, not a ratio)
- **Price basis:** list
- **Includes:** the price-list row in force on the invoice date for the item and unit
- **Excludes:** any price derived from invoices; when no list exists the metric is absent and dependent outputs say so
- **Amount status:** actual (from the mapped price-list reference table)

## Gross list value

What the baseline window would have billed at list: effective-dated list price times normalized quantity.

- **Formula:** list price in force on the invoice date x normalized quantity
- **Units:** cents
- **Denominator:** none (a dollar total)
- **Price basis:** list
- **Includes:** sale lines with an effective-dated list price
- **Excludes:** lines with no list price (coverage is reported beside the figure)
- **Amount status:** actual (list reference x actual quantities)

## Invoice product price

The realized unit price: net line amount divided by normalized quantity; billed charges are not part of it.

- **Formula:** net product amount / normalized quantity
- **Units:** cents per normalized selling unit
- **Denominator:** normalized quantity on the line
- **Price basis:** invoice (after on-invoice discounts)
- **Includes:** the product's own net amount, with on-invoice discounts already netted
- **Excludes:** allocated order-level billed charges (freight billed and similar), post-invoice rebates and concessions, and embedded taxes (carved out of the product amount)
- **Amount status:** actual

## Invoice revenue

Net invoiced amounts plus allocated order-level billed charges - what was billed, before post-invoice deductions.

- **Formula:** net product amount + allocated billed order charges
- **Units:** cents
- **Denominator:** none (a dollar total)
- **Price basis:** invoice
- **Includes:** net product amounts (on-invoice discounts already netted) and order-level billed charges allocated to lines by the documented driver
- **Excludes:** customer rebates, freight concessions, price credits (all post-invoice); taxes, deposits and third-party collections (carved out when embedded, never added when billed separately)
- **Amount status:** actual, plus allocated for order-level charges

## Pocket revenue

What is kept after post-invoice deductions: rebates, freight concessions and attributed price credits come off invoice revenue.

- **Formula:** invoice revenue - line-accrued rebates - allocated settlement rebates - freight concessions - attributed price credits
- **Units:** cents
- **Denominator:** none (a dollar total)
- **Price basis:** pocket
- **Includes:** every post-invoice deduction that scales with or attaches to the line
- **Excludes:** product cost and cost-to-serve (below this stage); vendor rebates (they adjust acquisition cost, never customer-facing revenue)
- **Amount status:** actual for line-accrued amounts; allocated for settlements; accrued rebate estimates are labeled estimated until trued up

## Pocket gross profit

Pocket revenue minus matched product cost, on lines whose cost is known - missing cost is never treated as zero.

- **Formula:** pocket revenue - product cost (declared basis, landed, net of vendor rebates)
- **Units:** cents
- **Denominator:** pocket revenue, when expressed as pocket gross margin; no margin is reported when the denominator is zero or negative
- **Price basis:** pocket
- **Includes:** lines with a known product cost on the declared cost basis
- **Excludes:** lines with missing cost (their revenue share is reported separately); cost-to-serve
- **Amount status:** actual cost basis; vendor rebate netting follows the declared semantics

## Contribution

Pocket gross profit minus attributable cost-to-serve: allocated freight expense, claims and return handling.

- **Formula:** pocket gross profit - attributable cost-to-serve
- **Units:** cents
- **Denominator:** pocket revenue, when expressed as a contribution margin
- **Price basis:** pocket
- **Includes:** allocated outbound freight expense, non-price claims, return handling and return freight expense
- **Excludes:** per-activity service estimates from config rates (those appear in contribution after activity cost); lines with missing product cost
- **Amount status:** allocated for freight and claims; estimated for policy-based return handling (labeled on each amount)

## Contribution after activity cost

Contribution minus estimated order-activity costs (per order, line and delivery from configured rates) - an estimate, labeled as one.

- **Formula:** contribution - activity cost estimates (per order + per line + per delivery)
- **Units:** cents
- **Denominator:** pocket revenue, when expressed as a margin
- **Price basis:** pocket
- **Includes:** configured activity rates applied to observed order, line and delivery counts
- **Excludes:** nothing further; this is the last stage of the customer economics view
- **Amount status:** estimated (config activity rates), always labeled

## Observed leakage

Level 1: reconciled dollars given up to discounts, rebates, concessions and price credits over the baseline window - an accounting fact, not a forecast.

- **Formula:** on-invoice discounts + accrued rebates + allocated settlement rebates + freight concessions + attributed price credits, summed over the baseline window
- **Units:** cents over the baseline window
- **Denominator:** none (a dollar total); leakage rate divides by net invoiced revenue
- **Price basis:** the gap between invoice and pocket
- **Includes:** every reconciled post-invoice deduction and on-invoice discount
- **Excludes:** product cost and cost-to-serve; never summed with level 2 or level 3 - the three levels answer different questions
- **Amount status:** actual and allocated (reconciled to source totals)

## Gross price opportunity

Level 2: the pocket value of closing the gap to the proposed price at unchanged volume - evidence does not support predicting a volume response.

- **Formula:** baseline quantity x positive invoice-price gap x (1 - d), where d is the pair's percentage post-invoice deduction rate
- **Units:** pocket cents over the baseline window
- **Denominator:** none (a dollar total)
- **Price basis:** invoice prices (index x effective list, or the labeled index fallback), converted to pocket with (1 - d)
- **Includes:** eligible pairs with a positive gap; a conservative variant raises below-P25 pairs only to P25 and is reported alongside
- **Excludes:** contract-protected pairs, insufficient-data guidance, any volume response; never summed with level 1 or level 3
- **Amount status:** computed from actual baselines and the guidance target

## Expected pocket gross profit change

Level 3 on a pocket gross profit basis: unit economics P x (1 - d) - F - C, with the causal-supported volume response.

- **Formula:** c(P1) x Q(P1) - c(P0) x Q(P0), with unit economics c(P) = P x (1 - d) - F - C and Q from the segment elasticity
- **Units:** pocket cents over the baseline window
- **Denominator:** none (a dollar total)
- **Price basis:** invoice price converted to pocket with (1 - d) and fixed deductions F
- **Includes:** pairs with known cost, causal-supported elasticity and a proposed move inside the segment's identifying price support
- **Excludes:** per-unit cost-to-serve K (see expected contribution change); pairs without cost; moves outside price support; never summed across levels
- **Amount status:** modeled from actual baselines and estimated elasticity
- **Pending change this run:** Phase 65 lands this as a metric distinct from the expected contribution change; today the engine computes this formula and labels it the expected contribution change.

## Expected contribution change

Level 3: contribution at the new price minus today's, with the causal-supported volume response - never summed with other levels.

- **Formula:** c(P1) x Q(P1) - c(P0) x Q(P0), with unit economics c(P) = P x (1 - d) - F - C today
- **Units:** pocket cents over the baseline window
- **Denominator:** none (a dollar total)
- **Price basis:** invoice price converted to pocket with (1 - d) and fixed deductions F
- **Includes:** pairs with known cost, causal-supported elasticity and a proposed move inside the segment's identifying price support; at instrumented-only evidence a labeled sensitivity range is shown instead
- **Excludes:** pairs without cost (missing cost is never zero); moves outside price support; never summed with level 1 or level 2
- **Amount status:** modeled from actual baselines and estimated elasticity
- **Pending change this run:** Phase 65 subtracts per-unit cost-to-serve K from the unit economics (c(P) = P x (1 - d) - F - C - K, K labeled estimated when it comes from config rates), making this a true contribution metric and renaming today's formula to the expected pocket gross profit change.
