"""Deterministic plain-English explanations (Phase 18).

Generated at export time from computed values only — no LLM, no API key.
Every number in an explanation comes from the row it explains; templates state
the evidence status and keep the honest-labels principles (reference bands are
historical positions, not optimal prices).
"""

from __future__ import annotations

from typing import Any

from margin_engine.labels import label as _label


def _idx(v: Any) -> str:
    return f"{float(v):.2f}" if v is not None else "unknown"


def _dollars(cents: Any) -> str:
    if cents is None:
        return "an unknown amount"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(int(cents)) / 100:,.2f}"


def guidance_explanation(row: dict[str, Any]) -> str:
    """Why this pair carries its guidance status, from its own numbers."""
    status = row.get("guidance_status") or "insufficient_data"
    cur = row.get("current_index")
    p25, p50, p75 = row.get("band_p25"), row.get("band_p50"), row.get("band_p75")
    floor_idx = row.get("floor_index")
    base = (
        f"This customer's realized price sits at {_idx(cur)} of the effective list "
        f"price. Comparable customers in the segment have historically paid between "
        f"{_idx(p25)} and {_idx(p75)}, with a midpoint of {_idx(p50)}."
    )
    if status == "below_floor":
        return (
            base + f" The price is below the economic floor (an index of {_idx(floor_idx)}), "
            "which is the lowest price that still earns the configured minimum pocket "
            "margin. This line earns less margin than policy requires. The floor is "
            "computed from cost and this customer's deduction rates, not from history."
        )
    if status == "below_reference":
        return (
            base + " The price is below that reference band: most comparable customers "
            "pay more for the same items. A band is a historical position, not an "
            "optimal price - it shows where this price is unusual, not what it "
            "should be."
        )
    if status == "above_reference":
        return (
            base + " The price is above that reference band: most comparable customers "
            "pay less. A position above the band is information, not automatically "
            "a problem - historical bands are not optimal prices."
        )
    if status == "in_band":
        return base + " The price sits inside the segment's reference band."
    if status == "market_reference_below_floor":
        return (
            base + f" The segment's historical midpoint sits below the economic floor "
            f"(an index of {_idx(floor_idx)}), so the market reference cannot be the "
            "target: pricing to history would earn less than the configured minimum "
            "margin. The floor is the target instead."
        )
    if status == "insufficient_pricing_headroom":
        return (
            base + f" The economic floor (an index of {_idx(floor_idx)}) sits at or above "
            "the reference band, so no price can satisfy both the floor and the "
            "band. The engine flags the conflict instead of relabeling the line."
        )
    return (
        "Too few comparable observations exist in this pair's segment to place it "
        "against a reference band, so the engine reports insufficient data rather "
        "than a confident-looking position."
    )


def opportunity_summary(row: dict[str, Any]) -> str:
    """One short, row-specific sentence stating what DRIVES the gap - the
    price position vs the proposed target - not the dollar figures already
    shown in the table columns."""
    cur = row.get("current_index")
    target = row.get("target_index")
    treatment = row.get("volume_treatment") or "fixed_volume_only"
    tail = {
        "expected_change": "causal evidence models the volume response.",
        "sensitivity_range_only": "instrument-level evidence limits the volume "
        "response to a labeled range.",
        "outside_price_support": "the move exceeds the segment's identified "
        "price range, so no volume response is modeled.",
        "cost_unavailable": "no unit cost is known, so cost-dependent figures are not produced.",
    }.get(treatment, "no volume response is modeled.")
    if cur is not None and target is not None and target > 0:
        below = (float(target) - float(cur)) / float(target)
        return (
            f"Priced {below:.0%} below the proposed target "
            f"({float(cur) * 100:.1f}% vs {float(target) * 100:.1f}% of list); {tail}"
        )
    return f"Gap to the proposed pocket price at unchanged volume; {tail}"


def opportunity_explanation(row: dict[str, Any]) -> str:
    """Why this opportunity is sized the way it is, from its own numbers."""
    gross = row.get("gross_price_opportunity_cents")
    expected_contrib = row.get("expected_contribution_change_cents")
    expected_gp = row.get("expected_pocket_gross_profit_change_cents")
    treatment = row.get("volume_treatment") or "fixed_volume"
    status = row.get("evidence_status") or "insufficient_data"
    base = (
        f"Closing the gap to the proposed pocket price is worth {_dollars(gross)} "
        "over the baseline window, assuming volume does not change."
    )
    if treatment == "expected_change" and expected_contrib is not None:
        return (
            base + f" This segment's elasticity is causal-supported, so the engine also "
            f"models the volume response: the expected contribution change is "
            f"{_dollars(expected_contrib)} (unit economics net of estimated "
            f"cost-to-serve; the pocket gross profit basis is "
            f"{_dollars(expected_gp) if expected_gp is not None else 'unavailable'})."
        )
    if treatment == "expected_change" and expected_gp is not None:
        return (
            base + f" This segment's elasticity is causal-supported, so the engine also "
            f"models the volume response: the expected pocket gross profit change is "
            f"{_dollars(expected_gp)}. No cost-to-serve estimate is supported for "
            f"this pair, so no contribution figure is shown."
        )
    if treatment == "sensitivity_range_only":
        return (
            base + f" The segment's elasticity evidence is {_label(status).lower()}, so "
            "the volume response is shown only as a labeled sensitivity range, not as "
            "a point estimate."
        )
    if treatment == "outside_price_support":
        return (
            base + " The proposed move lies outside the price variation the "
            "instruments ever induced in this segment, so the engine refuses to "
            "extrapolate a volume response - the figure assumes unchanged volume."
        )
    if treatment == "cost_unavailable":
        return (
            base + " No unit cost is known for this pair, so cost-dependent "
            "levels are not produced - missing cost is never treated as zero."
        )
    return (
        base + f" The segment's elasticity evidence is {_label(status).lower()}, so no "
        "volume response is modeled. Treat the figure as a ceiling at unchanged "
        "volume, not as a forecast."
    )
