"""Central label dictionary: every machine code that can reach a reader maps
to a display label and a one-line description. The web bundle embeds this
dictionary and the HTML report renders through it, so both read the same.
Raw codes may appear only in tooltips or technical-detail expanders.
"""

from __future__ import annotations

LABELS: dict[str, dict[str, str]] = {
    # --- evidence statuses (sensitivity, cross-price) ------------------------
    "causal_elasticity_supported": {
        "label": "Causal, supported",
        "description": "Every causal criterion passed: strong first stage, overidentification "
        "not rejected, robust across specifications, tight interval, documented "
        "exclusion rationale.",
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
        "description": "Fitted within the precision gate; accurate enough to gate and to "
        "inform win-aware optimization.",
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
    "provisional_historical": {
        "label": "Provisional (historical)",
        "description": "A historical position, not an optimal price - provisional until "
        "elasticity evidence supports optimization.",
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
    # --- volume treatments (opportunity) ----------------------------------------
    "expected_change": {
        "label": "Volume modeled",
        "description": "Causal-supported elasticity: the expected contribution change "
        "models the volume response.",
    },
    "sensitivity_range_only": {
        "label": "Range only",
        "description": "Instrument-level evidence: the volume response is shown only as a "
        "labeled sensitivity range.",
    },
    "fixed_volume_only": {
        "label": "Fixed volume",
        "description": "No supported elasticity: the figure assumes unchanged volume.",
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

_FEATURE_NOUNS: dict[str, tuple[str, str]] = {
    # feature -> (noun for IN phrasing, noun for NOT-IN phrasing)
    "industry": ("customers", "customers"),
    "size_band": ("accounts", "accounts"),
    "region": ("regions", "regions"),
    "channel": ("channel", "channel"),
    "contract_type": ("contracts", "contracts"),
    "item_family": ("items", "items"),
    "item_role": ("items", "items"),
    "order_size_band": ("orders", "orders"),
    "revenue_abc": ("revenue class", "revenue class"),
}

_VALUE_WORDS: dict[str, str] = {
    "kvi": "key-value",
    "long_tail": "long-tail",
    "strategic_contract": "strategic-contract",
    "specialty": "specialty",
    "A": "A",
    "B": "B",
    "C": "C",
}


def _nice_value(v: str) -> str:
    v = v.strip()
    return _VALUE_WORDS.get(v, v.replace("_", " "))


def _phrase(feature: str, values: list[str], negated: bool) -> str:
    noun = _FEATURE_NOUNS.get(feature, (feature.replace("_", " "), ""))[0]
    vals = " or ".join(_nice_value(v) for v in values)
    if feature == "revenue_abc":
        return f"revenue class {'not ' if negated else ''}{vals}"
    if feature in ("order_size_band", "size_band"):
        return f"{'not ' if negated else ''}{vals} {noun}"
    if negated:
        return f"{noun} outside {vals}"
    return f"{vals} {noun}"


def _parse_clause(clause: str) -> list[tuple[str, list[str], bool]]:
    """Parse one conjunction like 'industry IN {a, b} AND item_role NOT IN {c}'."""
    import re

    out = []
    for m in re.finditer(r"(\w+) (NOT IN|IN) \{([^}]*)\}", clause):
        feature, op, body = m.group(1), m.group(2), m.group(3)
        values = [v.strip() for v in body.split(",") if v.strip()]
        out.append((feature, values, op == "NOT IN"))
    return out


def plain_rule(rule: str) -> tuple[str, str]:
    """(short name, one-sentence description) derived from a tree rule string.
    Never hand-written: both come from the rule's own structure. Falls back to
    'All lines' / the raw rule when the rule has no parseable conditions."""
    if not rule or rule.strip() == "(all lines)":
        return "All lines", "Every line; the tree found no split for this leaf."
    clauses = [c.strip(" ()") for c in rule.split(" OR ")]
    parsed = [_parse_clause(c) for c in clauses]
    parsed = [p for p in parsed if p]
    if not parsed:
        # One-hot style rules ("feat" / "NOT feat") from the margin tree.
        conds = [c.strip() for c in rule.replace("(", "").replace(")", "").split(" AND ")]
        words = []
        for c in conds[:4]:
            neg = c.startswith("NOT ")
            words.append(("not " if neg else "") + c.removeprefix("NOT ").replace("_", " "))
        name = " · ".join(words[:3]) or "All lines"
        return name.capitalize(), "Lines where " + " and ".join(words) + "."
    first = parsed[0]
    name = " · ".join(_phrase(f, v, n) for f, v, n in first[:3])
    sentences = [" with ".join(_phrase(f, v, n) for f, v, n in conds) for conds in parsed]
    extra = f" (plus {len(parsed) - 1} similar group(s))" if len(parsed) > 1 else ""
    description = f"Lines from {sentences[0]}{extra}."
    return name.capitalize(), description
