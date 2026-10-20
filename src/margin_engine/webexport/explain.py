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
    """One short, row-specific sentence: the numbers and what drives them."""
    gross = row.get("gross_price_opportunity_cents")
    expected = row.get("expected_contribution_change_cents")
    treatment = row.get("volume_treatment") or "fixed_volume_only"
    if treatment == "expected_change" and expected is not None:
        return (
            f"{_dollars(gross)} gap at unchanged volume; causal evidence puts the "
            f"expected change at {_dollars(expected)} after the volume response."
        )
    if treatment == "sensitivity_range_only":
        return (
            f"{_dollars(gross)} gap at unchanged volume; instrument-level evidence "
            "limits the volume response to a labeled range."
        )
    return f"{_dollars(gross)} gap at unchanged volume; no volume response is modeled."


def opportunity_explanation(row: dict[str, Any]) -> str:
    """Why this opportunity is sized the way it is, from its own numbers."""
    gross = row.get("gross_price_opportunity_cents")
    expected = row.get("expected_contribution_change_cents")
    treatment = row.get("volume_treatment") or "fixed_volume"
    status = row.get("evidence_status") or "insufficient_data"
    base = (
        f"Closing the gap to the proposed pocket price is worth {_dollars(gross)} "
        "over the baseline window, assuming volume does not change."
    )
    if treatment == "expected_change" and expected is not None:
        return (
            base + f" This segment's elasticity is causal-supported, so the engine also "
            f"models the volume response: the expected contribution change is "
            f"{_dollars(expected)}."
        )
    if treatment == "sensitivity_range_only":
        return (
            base + f" The segment's elasticity evidence is {_label(status).lower()}, so "
            "the volume response is shown only as a labeled sensitivity range, not as "
            "a point estimate."
        )
    return (
        base + f" The segment's elasticity evidence is {_label(status).lower()}, so no "
        "volume response is modeled. Treat the figure as a ceiling at unchanged "
        "volume, not as a forecast."
    )
