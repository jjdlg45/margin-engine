"""Central label dictionary: every machine code that can reach a reader maps
to a display label and a one-line description. The web bundle embeds this
dictionary and the HTML report renders through it, so both read the same.
Raw codes may appear only in tooltips or technical-detail expanders.
"""

from __future__ import annotations

import re

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
    "outside_price_support": {
        "label": "Outside price support",
        "description": "The proposed move lies outside the price variation the instruments "
        "ever induced in this segment, so no volume response is modeled - the figure "
        "assumes unchanged volume.",
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
}

_VALUE_WORDS: dict[str, str] = {
    "kvi": "key-value",
    "long_tail": "long-tail",
    "strategic_contract": "strategic-contract",
}

MERGED_QUALIFIER = "[not assigned to a more specific leaf]"


def _nice_value(v: str) -> str:
    v = v.strip()
    return _VALUE_WORDS.get(v, v.replace("_", " "))


def _natural_list(values: list[str], conj: str = "and") -> str:
    vals = [_nice_value(v) for v in values]
    if len(vals) == 1:
        return vals[0]
    return ", ".join(vals[:-1]) + f" {conj} {vals[-1]}"


def _phrase(feature: str, values: list[str], negated: bool) -> str:
    noun = _FEATURE_NOUNS.get(feature, feature.replace("_", " "))
    if negated:
        # Exclusions read as "all X except A, B and C".
        return f"all {noun} except {_natural_list(values)}"
    if feature == "revenue_abc":
        return f"revenue class {_natural_list(values, 'or')}"
    if feature in ("order_size_band", "size_band"):
        return f"{_natural_list(values, 'or')} {noun}"
    if feature == "item_family":
        return f"{_natural_list(values, 'or')} {noun}"
    return f"{_natural_list(values, 'or')} {noun}"


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


def _feature_words(feature: str) -> tuple[str, str]:
    """(singular phrase stem, plural for 'all X except') for a feature."""
    if feature.endswith("_band"):
        stem = feature.removesuffix("_band").replace("_", " ")
        return stem, stem + "s"
    plural = _FEATURE_NOUNS.get(feature, feature.replace("_", " "))
    return feature.replace("_", " "), plural


def _one_hot_phrases(conds: list[str]) -> list[str]:
    """Group one-hot conditions per feature, phrasing exclusions with
    'except' exactly like the segment rules. An included value subsumes the
    same feature's exclusions (one-hot categories are mutually exclusive)."""
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
        stem, plural = _feature_words(feat)
        if feat in included:
            vals = _natural_list(included[feat], "or")
            phrases.append(f"{vals} {stem}" if feat.endswith("_band") else f"{stem} {vals}")
        elif feat in excluded:
            phrases.append(f"all {plural} except {_natural_list(sorted(excluded[feat]))}")
    return phrases


def _cap_first(text: str) -> str:
    """Capitalize only the first character; data values keep their case."""
    return text[:1].upper() + text[1:] if text else text


def plain_rule(rule: str) -> tuple[str, str, str]:
    """(short name, one-sentence description, technical note) derived from a
    tree rule string - never hand-written. The technical note carries
    merge/leaf mechanics that belong in expanders, not prose."""
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
            return (
                "Other lines",
                "Lines that fell out of leaves too small to report; no more "
                "specific grouping applies.",
                note,
            )
        return "All lines", "Every line; the tree found no split for this leaf.", note
    clauses = [c.strip(" ()") for c in core.split(" OR ")]
    parsed = [_parse_clause(c) for c in clauses]
    parsed = [p for p in parsed if p]
    if not parsed:
        # One-hot style rules from the margin tree.
        conds = [
            c.strip() for c in core.replace("(", "").replace(")", "").split(" AND ") if c.strip()
        ]
        words = _one_hot_phrases(conds)
        name = _cap_first(" · ".join(words))
        description = _cap_first(" and ".join(words) + ".")
        if merged_up:
            description += " A more specific leaf does not exist for these lines."
        return name or "All lines", description, note
    first = parsed[0]
    name = _cap_first(" · ".join(_phrase(f, v, n) for f, v, n in first[:3]))
    sentences = [" with ".join(_phrase(f, v, n) for f, v, n in conds) for conds in parsed]
    description = _cap_first(f"Lines from {sentences[0]}.")
    if len(parsed) > 1:
        note = (f"Merged from {len(parsed)} tree leaves. " + note).strip()
    if merged_up:
        description += " A more specific leaf does not exist for these lines."
    return name, description, note
