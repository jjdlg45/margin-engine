"""Central label dictionary: every machine code that can reach a reader maps
to a display label and a one-line description. The web bundle embeds this
dictionary and the HTML report renders through it, so both read the same.
Raw codes never reach a reader - not in visible text, tooltips or title
attributes; raw rule logic lives only in the CSV/Parquet exports.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from margin_engine.metrics import metric_label_entries

LABELS: dict[str, dict[str, str]] = {
    # --- segmentation features (Run 6, Phase 76) ----------------------------
    "abc_class": {
        "label": "ABC class",
        "description": "A, B or C by cumulative share of the configured basis, cut on a "
        "window that ends before the line's own period; an item with no history in that "
        "window is unrated.",
    },
    "order_size_band": {
        "label": "order size band",
        "description": "Small, medium or large by the order's quantity in normalized units. "
        "Cut on quantity rather than order value, so no realized price enters the feature.",
    },
    # --- evidence statuses (sensitivity, cross-price) ------------------------
    "causal_elasticity_supported": {
        "label": "Causal, supported",
        "description": "The prespecified diagnostics and documented assumptions passed for "
        "this estimand and scope; it does not prove the instruments are valid or that "
        "the result transfers to other customers, markets or times.",
    },
    "iv_estimated_elasticity": {
        "label": "Instrumented estimate",
        "description": "Instrumented with an adequate first stage, but at least one causal "
        "criterion failed - directional, not decision-grade.",
    },
    "descriptive_elasticity": {
        "label": "Descriptive only",
        "description": "An estimable association; no causal claim is supported.",
    },
    "insufficient_data": {
        "label": "Insufficient data",
        "description": "Too little data to support any estimate - the engine fails closed.",
    },
    "insufficient_precision": {
        "label": "Insufficient precision",
        "description": "The fit converged but its own standard error cannot support the "
        "claimed accuracy - reported, never gated.",
    },
    # --- win-curve statuses ---------------------------------------------------
    "estimated_win_curve": {
        "label": "Estimated win curve",
        "description": "Fitted within the precision gate: accurate enough to gate on, and "
        "to inform a win-aware price search where one is run.",
    },
    # --- guidance statuses ------------------------------------------------------
    "in_band": {
        "label": "In band",
        "description": "Inside the segment's historical reference band.",
    },
    "below_reference": {
        "label": "Below reference",
        "description": "Below the segment's historical reference band - most comparable "
        "customers pay more.",
    },
    "above_reference": {
        "label": "Above reference",
        "description": "Above the segment's historical reference band - most comparable "
        "customers pay less.",
    },
    "below_floor": {
        "label": "Below floor",
        "description": "Below the economic floor that guarantees the configured minimum "
        "pocket margin.",
    },
    "insufficient_pricing_headroom": {
        "label": "No pricing headroom",
        "description": "The floor sits at or above the reference band; no price satisfies both.",
    },
    "market_reference_below_floor": {
        "label": "Reference below floor",
        "description": "The segment's median historical price sits below the economic floor, "
        "so the market reference cannot be the target; the floor is.",
    },
    "pocket_gross_margin": {
        "label": "Gross-margin floor binds",
        "description": "The binding floor comes from the minimum pocket gross margin.",
    },
    "contribution_margin": {
        "label": "Contribution floor binds",
        "description": "The binding floor comes from the minimum contribution margin "
        "(includes estimated cost-to-serve).",
    },
    "effective_list": {
        "label": "Effective list",
        "description": "A valid effective-dated list price backs this pair's index comparisons.",
    },
    "stale_list": {
        "label": "Stale list",
        "description": "The list in force is older than the configured maximum age at the "
        "pair's last sale; dollar floors are kept, index comparisons are not made.",
    },
    "missing_or_zero_list": {
        "label": "No list price",
        "description": "No positive effective-dated list price: index-based comparisons are "
        "not made (a list price is never manufactured from the invoice price).",
    },
    "provisional_historical": {
        "label": "Provisional (historical)",
        "description": "The midpoint of what comparable customers have paid, clipped to the "
        "floor. A historical position, not an optimal price: this engine computes no "
        "optimal price, so this is the only kind of target a run produces.",
    },
    # --- amount statuses -----------------------------------------------------
    "actual": {"label": "Actual", "description": "Taken directly from the source export."},
    "allocated": {
        "label": "Allocated",
        "description": "Spread from a source total by the documented driver, exactly.",
    },
    "estimated": {
        "label": "Estimated",
        "description": "From configured policy rates, not source amounts - labeled wherever used.",
    },
    # --- action center (Run 5, Phases 38-42) ----------------------------------
    "price_change": {
        "label": "Price change",
        "description": "Raise one customer-item price toward the segment target, never "
        "below the floor.",
    },
    "below_floor_fix": {
        "label": "Below-floor fix",
        "description": "This pair is priced below the binding margin floor; raising to the "
        "floor is the minimum fix.",
    },
    "cost_pass_through": {
        "label": "Cost pass-through",
        "description": "Recover part of a cost increase at the chosen pass-through level; "
        "full pass-through is never assumed.",
    },
    "service_policy": {
        "label": "Service policy",
        "description": "A minimum-order or delivery-frequency change for a customer losing "
        "money after activity costs - not a price action.",
    },
    "rebate_discount_review": {
        "label": "Rebate and discount review",
        "description": "This customer's leakage rate sits far above its segment's median; "
        "the amount shown is the observed excess, never a forecast.",
    },
    "proposed": {
        "label": "Proposed",
        "description": "Generated or drafted; not yet submitted for review.",
    },
    "under_review": {
        "label": "Under review",
        "description": "Submitted by an analyst; waiting for an approver's decision.",
    },
    "approved": {
        "label": "Approved",
        "description": "An approver (never the proposer) accepted it; ready for the price export.",
    },
    "rejected": {
        "label": "Rejected",
        "description": "An approver declined it; it can be reworked and resubmitted.",
    },
    "exported": {
        "label": "Exported",
        "description": "Included in an ERP price export; waiting to be implemented.",
    },
    "implemented": {
        "label": "Implemented",
        "description": "Marked live in the ERP; the tracker measures it after the window.",
    },
    "measured": {
        "label": "Measured",
        "description": "The outcome window elapsed and the result is measured against an "
        "untreated comparison group.",
    },
    "high": {"label": "High", "description": "Largest expected impact with solid evidence."},
    "medium": {"label": "Medium", "description": "Meaningful impact or demoted evidence."},
    "low": {"label": "Low", "description": "Small expected impact or weak volume evidence."},
    "expected_contribution_change": {
        "label": "Expected contribution change",
        "description": "Level 3: contribution at the new price minus today's, with the "
        "causal-supported volume response - never summed with other levels.",
    },
    "gross_price_opportunity_fixed_volume": {
        "label": "Gross opportunity at unchanged volume",
        "description": "Level 2: the pocket value of the price gap if volume did not move - "
        "the evidence does not support predicting a response.",
    },
    "floor_gap_fixed_volume_pocket": {
        "label": "Floor gap at unchanged volume",
        "description": "The pocket dollars recovered by raising to the floor at today's volume.",
    },
    "no_recent_volume": {
        "label": "No recent volume",
        "description": "The pair had no baseline-window sales, so no dollar impact is invented.",
    },
    "cost_recovery_at_chosen_level": {
        "label": "Cost recovery at the chosen level",
        "description": "Dollars of the cost increase recovered at the configured "
        "pass-through percentage, at unchanged volume.",
    },
    "contribution_at_stake_estimated": {
        "label": "Contribution at stake (estimated)",
        "description": "The money lost after activity costs; activity rates are config estimates.",
    },
    "observed_leakage_excess": {
        "label": "Observed leakage excess",
        "description": "Level 1: reconciled dollars above the segment-median leakage rate - "
        "an accounting fact, never summed with price opportunities.",
    },
    "manual_proposal": {
        "label": "Manual proposal",
        "description": "Proposed by an analyst, not generated from engine outputs.",
    },
    "manual": {
        "label": "Manual",
        "description": "Entered by a person; no engine evidence attached.",
    },
    "insufficient_evidence": {
        "label": "Insufficient evidence",
        "description": "The outcome cannot be measured reliably; the reason is shown "
        "instead of a number.",
    },
    "causal_supported": {
        "label": "Causal-supported",
        "description": "Actions whose volume response met every causal criterion.",
    },
    "iv_estimated": {
        "label": "Instrumented estimate",
        "description": "Actions whose volume evidence was instrumented but not fully "
        "causal-supported.",
    },
    "fixed_volume": {
        "label": "Fixed volume",
        "description": "Actions sized at unchanged volume.",
    },
    "not_enough_implemented_actions": {
        "label": "Not enough implemented actions yet",
        "description": "Calibration appears once enough actions are implemented and "
        "measured; until then no number is shown.",
    },
    # --- pricing compliance (Run 5, Phase 37) ---------------------------------
    "rp_review_flag": {
        "label": "Price-discrimination review",
        "description": "Two likely competitors (same industry and region by default) carry "
        "recommended prices for the same item further apart than the configured "
        "threshold. A review prompt with the cost-to-serve difference attached, "
        "not a conclusion; approval requires a written justification.",
    },
    "cts_difference_cents": {
        "label": "Cost-to-serve difference",
        "description": "The difference in per-unit cost to serve between the two flagged "
        "customers - the first number a cost-justification review needs.",
    },
    # --- volume treatments (opportunity) ----------------------------------------
    "expected_change": {
        "label": "Volume modeled",
        "description": "Causal-supported elasticity: the expected contribution change "
        "models the volume response. It models how much buying customers buy, and holds "
        "steady the question of whether they keep buying at all; where the engine could "
        "estimate that second effect, it is shown beside this figure as an alternative.",
    },
    "sensitivity_range_only": {
        "label": "Range only",
        "description": "Instrument-level evidence: the volume response is shown as a "
        "sensitivity analysis - what the figure would be across a span of plausible price "
        "responses - and not as a confidence interval around the dollar impact. It also "
        "covers how much buying customers buy rather than whether they keep buying.",
    },
    "fixed_volume_only": {
        "label": "Fixed volume",
        "description": "No supported elasticity: the figure assumes unchanged volume.",
    },
    "outside_price_support": {
        "label": "Outside measured price movement",
        "description": "Cost and tariff movements never moved this segment's prices by "
        "anything like the amount proposed - or moved them either side of it but not at it "
        "- so there is nothing to base a volume response on. The figure assumes unchanged "
        "volume.",
    },
    # --- deduction treatments (Run 6, Phase 66) ---------------------------------
    "flat_rate_exact": {
        "label": "Flat rates, exact",
        "description": "Every percentage deduction applies independently on the same base "
        "(net invoiced value), so the flat rate is exact, not an approximation.",
    },
    "recomputed_tiered": {
        "label": "Tiered rebate recomputed",
        "description": "This customer holds a tiered program, so the rebate was recomputed "
        "at the candidate revenue - crossing a tier re-rates the whole basis.",
    },
    "fixed_rate_approximation": {
        "label": "Fixed-rate approximation",
        "description": "This customer's tiered arrangement has no usable tier table, so a "
        "flat rate stands in - labeled, never silent.",
    },
    "competitor_path_uncontrolled": {
        "label": "Competitor path uncontrolled",
        "description": "This segment's elasticity answers a market-wide question, so it is "
        "not applied to a unilateral price move: the figure assumes unchanged volume.",
    },
    "cost_unavailable": {
        "label": "Cost unavailable",
        "description": "No unit cost is known for this pair: cost-dependent figures are not "
        "produced (missing cost is never treated as zero).",
    },
    # --- service-policy flags -----------------------------------------------------
    "minimum_order_policy": {
        "label": "Minimum order policy",
        "description": "Orders average below the configured minimum value and the customer "
        "loses money after cost-to-serve.",
    },
    "delivery_frequency_policy": {
        "label": "Delivery frequency policy",
        "description": "More orders per month than the configured maximum at below-minimum "
        "average order value.",
    },
    "service_policy_not_pricing": {
        "label": "Service policy (not pricing)",
        "description": "A service-economics question, kept separate from pricing actions.",
    },
    # --- residual keys -----------------------------------------------------------
    "settlement_rebates_unallocated_cents": {
        "label": "Unallocated rebate settlements",
        "description": "Settled rebate dollars whose customer-period group has no invoice lines.",
    },
    "tiered_settlements_unallocated_cents": {
        "label": "Unallocated tiered settlements",
        "description": "Tiered-program settlements with no matching lines in the period.",
    },
    "tiered_true_up_cents": {
        "label": "Tiered rebate true-up",
        "description": "Timing difference between rebates accrued during the period and the "
        "amount settled at true-up.",
    },
    "return_freight_unallocated_cents": {
        "label": "Unallocated return freight",
        "description": "Return freight expense from the log that matched no return lines.",
    },
    "billed_charges_unallocated_cents": {
        "label": "Unallocated billed charges",
        "description": "Order-level charges whose order has no sale lines to carry them.",
    },
    "price_credits_unattributed_cents": {
        "label": "Unattributed price credits",
        "description": "Price-correction credits whose original invoice could not be identified.",
    },
    "freight_expense_unallocated_cents": {
        "label": "Unallocated freight expense",
        "description": "Outbound freight expense with no allocation basis in its group.",
    },
    "claims_unallocated_cents": {
        "label": "Unallocated claims",
        "description": "Non-price claim credits with no matching lines.",
    },
    "kit_realloc_net_cents": {
        "label": "Kit reallocation rounding",
        "description": "Net rounding of kit revenue reallocation across components; nonzero "
        "flags a problem to investigate.",
    },
    # --- reconciliation check names ---------------------------------------------
    "row_conservation": {
        "label": "Row conservation",
        "description": "Every source line is accounted for: ledger rows plus verified "
        "duplicates equal source rows.",
    },
    "net_amount_conservation": {
        "label": "Net amount conservation",
        "description": "Ledger net amounts sum exactly to the source export's total.",
    },
    "billed_charges_allocation": {
        "label": "Billed charges allocation",
        "description": "Order-level billed charges allocate exactly; the remainder is a "
        "visible residual.",
    },
    "settlement_rebate_allocation": {
        "label": "Settlement rebate allocation",
        "description": "Rebate settlements allocate exactly to matching lines; the rest is "
        "a visible residual.",
    },
    "freight_expense_allocation": {
        "label": "Freight expense allocation",
        "description": "Outbound freight expense allocates exactly by the documented driver.",
    },
    "kit_reallocation_zero_sum": {
        "label": "Kit reallocation zero-sum",
        "description": "Kit revenue reallocation nets to zero across components.",
    },
    "tiered_settlement_allocation": {
        "label": "Tiered settlement allocation",
        "description": "Tiered-program settlements allocate exactly; accrual vs settlement "
        "reconciles.",
    },
    "return_freight_allocation": {
        "label": "Return freight allocation",
        "description": "Return freight expense ties to return lines exactly.",
    },
    "pocket_stage_identity_sales": {
        "label": "Pocket stage identity",
        "description": "Invoice revenue minus post-invoice deductions equals pocket revenue "
        "on every sale line.",
    },
    "contribution_identity": {
        "label": "Contribution identity",
        "description": "Pocket revenue minus product cost minus cost-to-serve equals "
        "contribution on covered lines.",
    },
    # --- validation-world presets and method options (rendered inline in the
    # --- validation results table, so their labels read mid-sentence) -----------
    "persistent_demand": {
        "label": "persistent demand",
        "description": "Validation world where customer demand shocks persist across months "
        "(AR(1)) instead of arriving independently.",
    },
    "heterogeneous_elasticity": {
        "label": "heterogeneous elasticity",
        "description": "Misspecified validation world where the true elasticity varies "
        "across pairs within a segment; the recovered figure is a weighted average, "
        "not the planted value.",
    },
    "nonconstant_elasticity": {
        "label": "non-constant elasticity",
        "description": "Misspecified validation world where elasticity changes with the "
        "size of the price move, so no single constant elasticity exists.",
    },
    "competitor_comovement": {
        "label": "competitor co-movement",
        "description": "Misspecified validation world where competitor prices move with "
        "the same cost shocks as the instrument; estimates understate own-price "
        "sensitivity.",
    },
    "month_customer": {
        "label": "month × customer",  # noqa: RUF001
        "description": "Two-way clustering of standard errors by calendar month and by "
        "customer (the shipped default).",
    },
    "supplier_month": {
        "label": "supplier × month",  # noqa: RUF001
        "description": "Standard errors clustered at the level where the cost instrument "
        "varies: supplier by month.",
    },
    "real_data_validation": {
        "label": "real-data validation",
        "description": "The no-planted-truth checks (temporal backtest, placebo tests, "
        "rolling stability) run as a pipeline stage.",
    },
    # --- estimands and the competitor-price path (Run 6, Phase 70) --------------
    "unilateral_own_price_response": {
        "label": "Unilateral own-price response",
        "description": "The volume response to moving THIS price while observed "
        "competitor-price conditions are held constant - the quantity a pricing "
        "decision actually needs.",
    },
    "market_wide_cost_shock_response": {
        "label": "Market-wide cost-shock response",
        "description": "The volume response to an industry-wide cost movement, which also "
        "moved competitor prices. It understates how customers react to a unilateral "
        "increase, because they had nowhere cheaper to go.",
    },
    "descriptive_association": {
        "label": "Descriptive association",
        "description": "A measured relationship between price and quantity with no "
        "instrument behind it, so no causal reading is claimed.",
    },
    "understates_sensitivity": {
        "label": "Understates price sensitivity",
        "description": "The known direction of the remaining bias: the true unilateral "
        "response is at least as strong as the figure shown.",
    },
    "market_control_adequate": {
        "label": "Market index adequate",
        "description": "The public market price index is current, covers the estimation "
        "window, varies within each period and explains price movement, so it can hold "
        "competitor-price conditions constant.",
    },
    "market_control_absent": {
        "label": "No market index",
        "description": "No market price index is mapped, so competitor-price conditions are "
        "not held constant.",
    },
    "market_control_not_public": {
        "label": "Market index not public",
        "description": "A market price index is mapped but not declared a public source, so "
        "it is refused as a control.",
    },
    "market_control_stale": {
        "label": "Market index stale",
        "description": "The index stops before the estimation window ends, so it cannot "
        "describe current competitor-price conditions.",
    },
    "market_control_incomplete": {
        "label": "Market index incomplete",
        "description": "The index covers too few of the segment's estimation rows to hold "
        "conditions constant across it.",
    },
    "market_control_absorbed_by_time_effects": {
        "label": "Market index absorbed",
        "description": "The index takes one value per period, so the time fixed effects "
        "remove it exactly; it needs category or regional detail to control anything.",
    },
    "market_control_uninformative": {
        "label": "Market index uninformative",
        "description": "The index explains too little price movement to count as evidence of "
        "a competitor-price condition.",
    },
    "market_control_restates_instrument": {
        "label": "Market index restates an instrument",
        "description": "The index almost exactly repeats an instrument, so controlling for it "
        "would remove the identifying variation rather than a separate confound.",
    },
    "controlled_by_market_index": {
        "label": "Competitor path controlled",
        "description": "Every instrument that needs it has its competitor-price path held "
        "constant by the public market index.",
    },
    "no_competitor_price_path": {
        "label": "No competitor-price path",
        "description": "Every instrument's competitor-price path is documented as absent, "
        "with a written argument in configuration.",
    },
    "not_controlled": {
        "label": "Competitor path not controlled",
        "description": "At least one instrument's competitor-price path is neither controlled "
        "nor documented as absent, so the estimate answers a market-wide question.",
    },
    "control_coarser_than_instrument": {
        "label": "Market index too coarse",
        "description": "The index is coarser than an instrument's own variation, so it cannot "
        "absorb that instrument's competitor-price movement.",
    },
    # --- causal gates (Run 6, Phase 70) ----------------------------------------
    "exclusion_rationale_documented": {
        "label": "Exclusion argument documented",
        "description": "Every instrument carries a written economic argument for why it "
        "affects quantity only through price.",
    },
    "competitor_price_path_addressed": {
        "label": "Competitor-price path addressed",
        "description": "Competitor-price conditions are held constant, or every instrument's "
        "competitor-price path is documented as absent.",
    },
    "first_stage_strength": {
        "label": "First stage strong enough",
        "description": "The instruments move price hard enough for the estimate to mean "
        "something; the figure before controlling is shown beside it.",
    },
    "cluster_structure_adequate": {
        "label": "Enough independent clusters",
        "description": "Cost and tariff movements are assigned to whole suppliers and "
        "categories, so those are the units that count as independent - not the number of "
        "invoice lines. Too few of them, and the interval would look far more precise than "
        "the evidence is.",
    },
    "overidentification_not_rejected": {
        "label": "Overidentification not rejected",
        "description": "The two instruments agree. This test cannot detect a violation they "
        "both share.",
    },
    "robust_across_specifications": {
        "label": "Robust across specifications",
        "description": "Using each instrument alone, and the alternatives, lands close enough "
        "to the headline estimate.",
    },
    "imputed_price_robustness": {
        "label": "Robust to imputed no-purchase prices",
        "description": "A month with no purchase has no invoiced price, so the engine fills "
        "one in. Including those months does not move the estimate materially.",
    },
    "interval_precise_enough": {
        "label": "Interval precise enough",
        "description": "The 95% interval is narrow enough to act on.",
    },
    "weak_instrument_robust_agreement": {
        "label": "Weak-instrument-robust agreement",
        "description": "The weak-instrument-robust set is bounded and agrees with the primary "
        "interval.",
    },
    # --- cluster structure (Run 6, Phase 73) -----------------------------------
    "cluster_supplier": {
        "label": "Supplier",
        "description": "Each supplier counts as one independent group, however many months "
        "and lines it covers. This is where the cost instrument is assigned.",
    },
    "cluster_supplier_month": {
        "label": "Supplier and month",
        "description": "Each supplier-month counts as one independent group. Only adequate "
        "when a supplier's cost movements are unrelated from one month to the next.",
    },
    "cluster_category": {
        "label": "Product category",
        "description": "Each product category counts as one independent group. This is where "
        "the tariff instrument is assigned.",
    },
    "cluster_category_month": {
        "label": "Product category and month",
        "description": "Each category-month counts as one independent group. Only adequate "
        "when a category's tariff movements are unrelated from one month to the next.",
    },
    "cluster_month": {
        "label": "Month",
        "description": "Each month counts as one independent group, which allows for anything "
        "that moves all customers together within a month.",
    },
    "cluster_month_customer": {
        "label": "Month and customer",
        "description": "Allows both for shocks that hit every customer in a month and for a "
        "customer's own demand carrying over between months.",
    },
    "interval_cluster_robust_t": {
        "label": "Group-robust interval",
        "description": "The standard interval, which relies on there being enough independent "
        "groups for its approximation to hold.",
    },
    "interval_wild_cluster_bootstrap": {
        "label": "Resampled interval",
        "description": "With few independent groups the standard interval is too narrow, so "
        "the interval is rebuilt by repeatedly re-running the estimate with the groups "
        "reweighted at random.",
    },
    "interval_withdrawn_few_clusters": {
        "label": "Interval withdrawn",
        "description": "There are too few independent groups for any interval to be "
        "trustworthy, so none is reported. The estimate itself is still shown.",
    },
    # --- results tracking (Run 6, Phase 75) ------------------------------------
    "no_pretrend_detected": {
        "label": "No earlier movement found",
        "description": "Treated customers were not already moving before the price did, as "
        "far as this data can see. Which is not the same as a guarantee: the check reports "
        "what size of earlier movement it could have detected.",
    },
    "pretrend_detected": {
        "label": "Already moving beforehand",
        "description": "Treated customers' volumes were already moving before the price "
        "changed, so what happened afterwards cannot be read as an effect of the price.",
    },
    "pretrend_not_testable": {
        "label": "Could not be checked",
        "description": "Only one period before the change is in the window, so there is "
        "nothing to compare against - which is not the same as there being nothing there.",
    },
    "fully_realized": {
        "label": "Price moved in full",
        "description": "The invoiced price moved all the way to the new price.",
    },
    "partly_realized": {
        "label": "Price moved part way",
        "description": "The invoiced price moved toward the new price but did not reach it.",
    },
    "not_realized": {
        "label": "Price did not move",
        "description": "The invoiced price stayed where it was, so this action says nothing "
        "about how customers respond to a price change.",
    },
    "moved_the_other_way": {
        "label": "Price moved the other way",
        "description": "The invoiced price moved away from the new price rather than toward it.",
    },
    "not_known": {
        "label": "Not known",
        "description": "No invoice after the change, so there is no invoiced price to compare "
        "- which happens when the customer stopped buying.",
    },
    "pass": {"label": "Passed", "description": "This criterion was met."},
    "fail": {"label": "Not met", "description": "This criterion was not met."},
    "not_evaluated": {
        "label": "Not evaluated",
        "description": "The data could not support this check, so it is not counted as passed.",
    },
    # --- weak-instrument diagnostics (Run 6, Phase 71) --------------------------
    "ar_set_empty": {
        "label": "Robust set empty",
        "description": "No candidate price sensitivity survives the weak-instrument-robust "
        "test: the instruments reject the model everywhere searched, which is evidence of "
        "misspecification rather than precision.",
    },
    "ar_set_single_point": {
        "label": "Robust set a single point",
        "description": "Only one candidate value survives, so the model is rejected almost "
        "everywhere - evidence against it, not a tight estimate.",
    },
    "ar_set_interval": {
        "label": "Robust set one range",
        "description": "The weak-instrument-robust set is a single connected range.",
    },
    "ar_set_disconnected": {
        "label": "Robust set disconnected",
        "description": "The robust set is two or more separate ranges with rejected values "
        "between them, so no single interval describes it.",
    },
    "ar_set_reaches_search_bound": {
        "label": "Robust set open-ended",
        "description": "The robust set runs to the edge of the searched range, so it is "
        "reported as open rather than compressed into an interval.",
    },
    "lead_test_passed": {
        "label": "No anticipation detected",
        "description": "Future cost and tariff movements do not predict current volume, and "
        "the interval is tight enough for that to mean something. It tests TIMING only: a "
        "violation both instruments share in the same period stays invisible to it.",
    },
    "lead_test_rejected": {
        "label": "Anticipation detected",
        "description": "Future cost and tariff movements predict current volume, which is an "
        "exclusion violation the overidentification test cannot see.",
    },
    "lead_test_immaterial": {
        "label": "Anticipation detected but immaterial",
        "description": "Future cost and tariff movements do predict current volume, but "
        "every effect the interval admits is smaller than the engine treats as material. "
        "With tens of thousands of rows this check resolves effects far below that bar, "
        "so it is reported rather than treated as a broken assumption.",
    },
    "lead_test_inconclusive": {
        "label": "Anticipation test inconclusive",
        "description": "No anticipation effect was detected, but the interval still admits a "
        "material one: the test lacks the power to clear the channel, so it is not reported "
        "as a pass.",
    },
    "lead_test_unavailable": {
        "label": "Anticipation test not run",
        "description": "Too few periods, clusters or complete rows to test anticipation, so "
        "nothing is claimed about it either way.",
    },
    "no_material_anticipation_effect": {
        "label": "No material anticipation effect",
        "description": "Future instrument movements do not predict current volume by a "
        "margin the engine treats as material. A material effect blocks the causal "
        "status; a detected but immaterial one is reported and does not block; an "
        "inconclusive result is reported with the smallest effect the test could have "
        "detected.",
    },
    "price_response_shape_untested": {
        "label": "Price-response shape untested; volume held steady",
        "description": "The move is inside the price range this segment's cost and tariff "
        "movements cover, but outside the narrower band the engine holds a segment to when "
        "it could not test whether one elasticity describes the whole segment. The figure "
        "assumes volume does not change.",
    },
    "provisional_shape_untested": {
        "label": "Provisional target: price-response shape untested",
        "description": "No optimal price is calculated for this segment. An optimizer picks "
        "the price that maximizes a curve, and the shape of this segment's curve could not "
        "be tested - the comparison between its cheaper-priced and dearer-priced pairs was "
        "inconclusive - so the historical reference target stands instead.",
    },
    "constant_elasticity_supported": {
        "label": "One elasticity fits the whole segment",
        "description": "The same instrumented estimate was run on the segment's "
        "cheaper-priced pairs and its dearer-priced pairs, and the two agree. Pricing a "
        "move off a single elasticity assumes exactly that; where the halves disagree, "
        "the estimate is a local average around the prices in the data and the causal "
        "status is withheld.",
    },
    "price_dependent_elasticity_detected": {
        "label": "Price sensitivity changes with the price level",
        "description": "The cheaper-priced and dearer-priced halves of this segment give "
        "different elasticities, so one number does not describe it. The estimate is "
        "still reported as an instrumented local average, but it cannot carry the causal "
        "status, because a move to a price the data barely contains would be priced off "
        "an elasticity that does not hold there.",
    },
    "price_response_shape_untested_badge": {
        "label": "Price-response shape untested",
        "description": "The comparison between this segment's cheaper-priced and "
        "dearer-priced pairs could not resolve whether one elasticity describes it. The "
        "estimate stands and keeps its evidence status, but no optimal price is calculated "
        "for the segment, and a modeled volume response is only produced for moves inside "
        "a narrowed band around the prices the instruments actually moved.",
    },
    "functional_form_inconclusive": {
        "label": "Could not test whether one elasticity fits",
        "description": "The segment could not be split into two halves that each support "
        "their own instrumented estimate - too few rows, too few pairs, or too little "
        "instrument movement inside a half. Nothing is claimed either way, and the "
        "causal status is not awarded on the strength of a test that could not speak.",
    },
    "functional_form_not_tested": {
        "label": "Functional form not tested",
        "description": "The comparison between cheaper-priced and dearer-priced pairs was "
        "not run. Usually the segment never reached an instrumented estimate for the "
        "comparison to be made on; it can also mean the check is switched off in config, "
        "or that the data carries no item identifier to place a price against.",
    },
    "unclassified_volume_treatment": {
        "label": "Volume treatment not classified",
        "description": "This action's volume treatment has no evidence bucket, so its "
        "prediction error is reported separately rather than pooled with a bucket that "
        "would describe it wrongly.",
    },
    "insufficient_data_total_quantity": {
        "label": "Total quantity: not enough evidence",
        "description": "There was not enough to estimate how total quantity responds to "
        "price, counting months with no purchase. Only the response among buying "
        "customers is reported.",
    },
    "descriptive_total_quantity": {
        "label": "Total quantity: descriptive only",
        "description": "A total-quantity response was estimated but not on strong enough "
        "instruments, or on a panel where too many of the no-purchase months carry an "
        "imputed price, so it describes an association rather than a response to price.",
    },
    "iv_estimated_total_quantity": {
        "label": "Total quantity: instrumented estimate",
        "description": "How total quantity responds to price, including whether the "
        "customer buys at all. This is the highest status this measure can reach: on test "
        "worlds it recovers the planted response about six times less precisely than the "
        "buying-customers measure recovers its own, it has no cross-check against a second "
        "instrument, and the out-of-sample test does not cover it. The engine never prices "
        "from it.",
    },
    "placebo_not_run": {
        "label": "Placebo check not run",
        "description": "The shuffled-instrument placebo needs the validation run, so "
        "nothing is claimed here about whether this design's standard errors are the "
        "right size.",
    },
    "placebo_passed": {
        "label": "Placebo check passed",
        "description": "Shuffled instruments reached significance at about the nominal "
        "rate, so this design's standard errors are the right size. It says nothing "
        "about whether the real instruments are valid.",
    },
    "placebo_underpowered": {
        "label": "Placebo check not conclusive",
        "description": "Shuffled instruments did not reach significance more often "
        "than chance, but too few draws were run for that to clear this design's "
        "standard errors. The smallest over-rejection the draw count could have "
        "caught is reported.",
    },
    "placebo_over_rejects": {
        "label": "Placebo check over-rejects",
        "description": "Shuffled instruments reached significance far more often than "
        "they should, so the standard errors in this design are too small and a "
        "significant result on the real instruments is not trustworthy either.",
    },
    "overidentification_blind_spot": {
        "label": "What overidentification cannot see",
        "description": "The overidentification test compares the instruments against each "
        "other, so it detects disagreement - never a violation they share. Two industry-wide "
        "cost instruments can both fail the same way and the test still passes.",
    },
    "anderson_rubin_blind_spot": {
        "label": "What the robust set cannot fix",
        "description": "The Anderson-Rubin set stays valid when instruments are weak, but it "
        "cannot repair a wrong exclusion story: it answers 'given these instruments', not "
        "'are these instruments right'.",
    },
    # --- economic event taxonomy (Run 6, Phase 61) ------------------------------
    "customer_price_concession": {
        "label": "Customer price concession",
        "description": "Discounts, rebates, freight concessions and price credits that "
        "reduce what the customer actually pays.",
    },
    "customer_refund_return": {
        "label": "Customer refund or return",
        "description": "Product returned by the customer; linked to the original sale "
        "where the data allows.",
    },
    "customer_service_payment": {
        "label": "Customer service payment",
        "description": "Restocking fees and return freight billed to the customer - "
        "recovery revenue, not a price concession.",
    },
    "non_price_customer_claim": {
        "label": "Non-price customer claim",
        "description": "Damage, shortage, quality or warranty credits - a cost, never a "
        "reduction of realized price.",
    },
    "supplier_rebate_allowance": {
        "label": "Supplier rebate or allowance",
        "description": "Vendor consideration that reduces acquisition cost, never "
        "customer-facing revenue.",
    },
    "supplier_quality_claim": {
        "label": "Supplier quality claim",
        "description": "A recovery from a supplier for supplier-caused cost; reduces "
        "cost to serve, with the supplier as counterparty.",
    },
    "vendor_distinct_service_reimbursement": {
        "label": "Vendor payment for a distinct service",
        "description": "Vendor consideration that pays for a distinct good or service - "
        "an ASC 705-20 exception that never nets product cost.",
    },
    "vendor_selling_cost_reimbursement": {
        "label": "Vendor selling-cost reimbursement",
        "description": "Vendor consideration reimbursing identifiable selling costs - an "
        "ASC 705-20 exception that never nets product cost.",
    },
    "freight_pass_through": {
        "label": "Freight pass-through",
        "description": "A billed charge the configuration declares as freight passed "
        "through to the customer.",
    },
    "tax_deposit_liability": {
        "label": "Tax, deposit or third-party collection",
        "description": "Amounts collected for someone else; excluded from product "
        "revenue by default.",
    },
    "other_adjustment": {
        "label": "Other adjustment",
        "description": "An adjustment without a declared classification - stated, never "
        "silently absorbed.",
    },
    "partially_settled": {
        "label": "Partially settled",
        "description": "Some of this customer-year's rebate programs settled to actuals; "
        "the rest remain accrual estimates.",
    },
    "fail_closed_negative_basis": {
        "label": "Fail closed: negative basis",
        "description": "Returns exceeded purchases in the period, so no rebate accrual "
        "is invented; the case is reported instead.",
    },
    "unallocated_residual": {
        "label": "Unallocated residual",
        "description": "A source amount with no matching lines to carry it; kept as a "
        "visible residual, never forced onto lines.",
    },
    # --- monthly GL variance check (Run 6, Phase 64) ----------------------------
    "gl_exact": {
        "label": "GL exact",
        "description": "Every compared month-measure matches the general ledger to the cent.",
    },
    "gl_within_tolerance": {
        "label": "GL within tolerance",
        "description": "Every compared month-measure sits inside the configured variance "
        "tolerance; the variances are shown, never hidden.",
    },
    "gl_variances_outside_tolerance": {
        "label": "GL variances outside tolerance",
        "description": "At least one month-measure differs from the general ledger beyond "
        "the tolerance - flagged for review, never suppressed.",
    },
    "gl_not_supplied": {
        "label": "Not testable (no GL supplied)",
        "description": "No general-ledger totals were mapped, so the variance check could "
        "not run; source-total reconciliation remains exact and always on.",
    },
    "source_totals_reconciled": {
        "label": "Source totals reconciled",
        "description": "Ledger totals tie to the source export exactly - the always-on "
        "check the GL variance control builds on.",
    },
    # --- statuses used by the report/UI for reconciliation ---------------------
    "reconciled": {
        "label": "Reconciled",
        "description": "Ledger totals equal source totals exactly after rounding.",
    },
    "reconciliation_failed": {
        "label": "Reconciliation failed",
        "description": "At least one reconciliation check did not tie to source totals.",
    },
}

# Metric definitions (Run 6, 60.1) are authored once in margin_engine.metrics
# and merged here, so the web bundle, the HTML report and the generated
# docs/metric-dictionary.md all serve the same words.
LABELS.update(metric_label_entries())


_CODE_TOKEN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def render_text(text: str) -> str:
    """Replace every known machine code in free text with its display label,
    so strings written for maintainers (validation.json) never reach a reader
    raw. Unknown snake_case tokens are left untouched ON PURPOSE: the web
    suite's raw-code gate must fail loudly on a new code so it gets a label
    here, rather than being prettified silently."""
    return _CODE_TOKEN.sub(
        lambda m: LABELS[m.group(0)]["label"] if m.group(0) in LABELS else m.group(0), text
    )


def label(code: str) -> str:
    """Display label for a code; unknown codes prettify instead of leaking."""
    entry = LABELS.get(code)
    if entry:
        return entry["label"]
    return code.replace("_", " ").replace("-", " ").capitalize()


def describe(code: str) -> str:
    entry = LABELS.get(code)
    return entry["description"] if entry else ""


# ---------------------------------------------------------------------------
# Segment-rule translation: plain English generated from the rule structure.
# ---------------------------------------------------------------------------

_FEATURE_NOUNS: dict[str, str] = {
    "industry": "customers",
    "size_band": "accounts",
    "region": "regions",
    "channel": "channels",
    "contract_type": "contracts",
    "item_family": "item families",
    "item_role": "items",
    "order_size_band": "orders",
    "revenue_abc": "revenue class",
    "abc_class": "items",
}

_VALUE_WORDS: dict[str, str] = {
    "kvi": "key-value",
    "long_tail": "rarely ordered",
    "strategic_contract": "strategic-contract",
    "mid": "medium",
}

# Engine-constructed ordered bands, in level order. A negated condition on one
# of these is rendered as the INCLUDED levels ("low or medium discounts"),
# never as a negation ("all discounts except high"). Only engine-constructed
# features belong here: their level universe is fixed by code, so the
# complement is always known. Data-driven categoricals keep "all X except ..."
# because their universe comes from the data.
_ORDERED_LEVELS: dict[str, list[str]] = {
    "discount_pct_band": ["low", "mid", "high"],
    "rebate_rate_band": ["low", "mid", "high"],
    "cost_ratio_band": ["low", "mid", "high"],
    "order_size_band": ["small", "medium", "large"],
    "revenue_abc": ["A", "B", "C"],
    # "unrated" is a level, not a gap: an item with no history in the lookback
    # window has no class, and saying so beats quietly calling it a C.
    "abc_class": ["A", "B", "C", "unrated"],
}

# Band features whose plain phrase is not "<levels> <noun>".
_BAND_PHRASES: dict[str, str] = {
    "discount_pct_band": "discounts",
    "rebate_rate_band": "rebate rates",
    "cost_ratio_band": "cost relative to price",
}

MERGED_QUALIFIER = "[not assigned to a more specific leaf]"


def display_value(v: str, acronyms: frozenset[str] = frozenset()) -> str:
    """Display form of a categorical DATA value (Phase 46): each word
    capitalized, acronyms (from the mapping's display.acronyms list) fully
    upper-cased. Raw values stay raw in every CSV/Parquet export."""
    upper = {a.upper() for a in acronyms}
    words = v.replace("_", " ").split(" ")
    out = [w.upper() if w.upper() in upper else (w[:1].upper() + w[1:]) for w in words if w]
    return " ".join(out)


def _nice_value(v: str, acronyms: frozenset[str] = frozenset(), *, plain: bool = False) -> str:
    v = v.strip()
    # Engine-constructed descriptor words and band levels keep their sentence
    # case (plain=True); actual DATA values render through display_value
    # (Phase 46): capitalized words, declared acronyms upper-case.
    if v in _VALUE_WORDS:
        return _VALUE_WORDS[v]
    if plain:
        return v.replace("_", " ")
    return display_value(v, acronyms)


def _natural_list(
    values: list[str],
    conj: str = "and",
    acronyms: frozenset[str] = frozenset(),
    *,
    plain: bool = False,
) -> str:
    vals = [_nice_value(v, acronyms, plain=plain) for v in values]
    if len(vals) == 1:
        return vals[0]
    return ", ".join(vals[:-1]) + f" {conj} {vals[-1]}"


def _order_values(feature: str, values: list[str]) -> list[str]:
    order = _ORDERED_LEVELS.get(feature)
    if order and all(v in order for v in values):
        return sorted(values, key=order.index)
    return sorted(values)


def _include_phrase(feature: str, values: list[str], acronyms: frozenset[str] = frozenset()) -> str:
    # Engine-constructed ordered bands are sentence words, not data values.
    plain = feature in _ORDERED_LEVELS
    vals = _natural_list(_order_values(feature, values), "or", acronyms, plain=plain)
    if feature in _BAND_PHRASES:
        return f"{vals} {_BAND_PHRASES[feature]}"
    if feature == "revenue_abc":
        return f"revenue class {vals}"
    if feature == "abc_class":
        return f"ABC class {vals}"
    noun = _FEATURE_NOUNS.get(feature, feature.replace("_", " "))
    return f"{vals} {noun}"


def _phrase(
    feature: str, values: list[str], negated: bool, acronyms: frozenset[str] = frozenset()
) -> str:
    if not negated:
        return _include_phrase(feature, values, acronyms)
    order = _ORDERED_LEVELS.get(feature)
    if order and set(values) <= set(order):
        kept = [lv for lv in order if lv not in values]
        if kept:
            return _include_phrase(feature, kept, acronyms)
    # Data-driven categories: exclusions read as "all X except A, B and C".
    noun = _FEATURE_NOUNS.get(feature, feature.replace("_", " "))
    plain = feature in _ORDERED_LEVELS
    return f"all {noun} except {_natural_list(sorted(values), acronyms=acronyms, plain=plain)}"


def _parse_clause(clause: str) -> list[tuple[str, list[str], bool]]:
    """Parse one conjunction like 'industry IN {a, b} AND item_role NOT IN {c}'."""
    import re

    out = []
    for m in re.finditer(r"(\w+) (NOT IN|IN) \{([^}]*)\}", clause):
        feature, op, body = m.group(1), m.group(2), m.group(3)
        values = [v.strip() for v in body.split(",") if v.strip()]
        out.append((feature, values, op == "NOT IN"))
    return out


_ONE_HOT_FEATURES = (
    "item_family",
    "item_role",
    "order_size_band",
    "size_band",
    "industry",
    "region",
    "channel",
    "contract_type",
    "revenue_abc",
    "abc_class",
)


def _split_one_hot(cond: str) -> tuple[str, str, bool]:
    """'NOT cost_ratio_band_low' -> ('cost_ratio_band', 'low', True)."""
    neg = cond.startswith("NOT ")
    body = cond.removeprefix("NOT ")
    if "_band_" in body:
        feat, val = body.rsplit("_band_", 1)
        return feat + "_band", val, neg
    for feat in _ONE_HOT_FEATURES:
        if body.startswith(feat + "_"):
            return feat, body[len(feat) + 1 :], neg
    return body, "", neg


def _one_hot_phrases(conds: list[str], acronyms: frozenset[str] = frozenset()) -> list[str]:
    """Group one-hot conditions per feature, rendering each group through the
    same phrasing as the segment rules: ordered bands always as included
    levels, data-driven categories excluded with 'except'. An included value
    subsumes the same feature's exclusions (one-hot categories are mutually
    exclusive)."""
    included: dict[str, list[str]] = {}
    excluded: dict[str, list[str]] = {}
    order: list[str] = []
    for cond in conds:
        feat, val, neg = _split_one_hot(cond)
        if feat not in order:
            order.append(feat)
        (excluded if neg else included).setdefault(feat, []).append(val)
    phrases = []
    for feat in order:
        if feat in included:
            phrases.append(_include_phrase(feat, included[feat], acronyms))
        elif feat in excluded:
            phrases.append(_phrase(feat, sorted(excluded[feat]), negated=True, acronyms=acronyms))
    return phrases


def _cap_first(text: str) -> str:
    """Capitalize only the first character; data values keep their case."""
    return text[:1].upper() + text[1:] if text else text


class PlainRule(NamedTuple):
    """Reader-facing rendering of one tree rule, derived - never hand-written.

    ``conditions`` is the plain-English bullet list behind "How this group is
    defined"; ``note`` carries merge/leaf mechanics. The raw rule string stays
    in the CSV/Parquet exports only.
    """

    name: str
    description: str
    conditions: list[str]
    note: str


_MERGED_BULLET = (
    "No more specific group exists for these lines: they fell out of groups "
    "below the reporting minimums."
)


def plain_rule(rule: str, acronyms: frozenset[str] = frozenset()) -> PlainRule:
    """Plain-English name, description and condition bullets for a tree rule.

    A segment merged from several tree leaves (an OR of branch rules) is
    covered in FULL: the name and the description mention every branch, and
    the bullets list every branch's conditions - never just the first branch.
    """
    rule = rule.strip()
    merged_up = MERGED_QUALIFIER in rule
    core = rule.replace(MERGED_QUALIFIER, "").strip()
    note = ""
    if merged_up:
        note = (
            "Holds lines merged up from leaves below the reporting minimums; "
            "the conditions hold but a more specific leaf does not exist."
        )
    if not core or core == "(all lines)":
        if merged_up:
            return PlainRule(
                "Other lines",
                "Lines that fell out of leaves too small to report; no more "
                "specific grouping applies.",
                [_MERGED_BULLET],
                note,
            )
        return PlainRule(
            "All lines",
            "Every line; the tree found no split for this leaf.",
            ["Every line in the data; the tree found no condition that sets this group apart."],
            note,
        )
    clauses = [c.strip(" ()") for c in core.split(" OR ")]
    parsed = [p for p in (_parse_clause(c) for c in clauses) if p]
    if not parsed:
        # One-hot style rules from the margin tree.
        conds = [
            c.strip() for c in core.replace("(", "").replace(")", "").split(" AND ") if c.strip()
        ]
        words = _one_hot_phrases(conds, acronyms)
        name = _cap_first(" · ".join(words))
        description = _cap_first(" and ".join(words) + ".")
        bullets = [_cap_first(w) for w in words]
        if merged_up:
            description += " A more specific leaf does not exist for these lines."
            bullets.append(_MERGED_BULLET)
        return PlainRule(name or "All lines", description, bullets, note)
    sentences = [
        " with ".join(_phrase(f, v, n, acronyms) for f, v, n in branch) for branch in parsed
    ]
    # The name covers every branch (each branch contributes its leading
    # conditions); the description and bullets carry every branch in full.
    name_parts: list[str] = []
    for branch in parsed:
        part = " with ".join(_phrase(f, v, n, acronyms) for f, v, n in branch[:3])
        if part not in name_parts:
            name_parts.append(part)
    name = _cap_first(" or ".join(name_parts))
    description = _cap_first(
        f"Lines from {sentences[0]}" + "".join(f"; or from {s}" for s in sentences[1:]) + "."
    )
    if len(parsed) == 1:
        bullets = [_cap_first(_phrase(f, v, n, acronyms)) for f, v, n in parsed[0]]
    else:
        bullets = [_cap_first(sentences[0]), *(f"or {s}" for s in sentences[1:])]
    if len(parsed) > 1:
        note = (f"Merged from {len(parsed)} tree leaves. " + note).strip()
    if merged_up:
        description += " A more specific leaf does not exist for these lines."
        bullets.append(_MERGED_BULLET)
    return PlainRule(name, description, bullets, note)
