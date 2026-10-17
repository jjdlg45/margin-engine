"""Deterministic plain-English explanations (Phase 18).

Generated at export time from computed values only — no LLM, no API key.
Every number in an explanation comes from the row it explains; templates state
the evidence status and keep the honest-labels principles (reference bands are
historical positions, not optimal prices).
"""

from __future__ import annotations

from typing import Any


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
        f"This customer's realized price sits at {_idx(cur)} of effective list; "
        f"the segment's historical reference band is {_idx(p25)} to {_idx(p75)} "
        f"with a midpoint of {_idx(p50)}."
    )
    if status == "below_floor":
        return (
            base + f" It is below the economic floor (index {_idx(floor_idx)}), the price "
            "at which the configured minimum pocket margin is still earned, so the "
            "line loses margin policy requires. Floors are computed from cost and "
            "this customer's deduction rates, not from history."
        )
    if status == "below_reference":
        return (
            base + " It is below the segment's reference band: most comparable "
            "customers pay more for the same items. The band is a historical "
            "position, not an optimal price; it shows where this price is "
            "unusual, not what it should be."
        )
    if status == "above_reference":
        return (
            base + " It is above the segment's reference band: most comparable "
            "customers pay less. Historical bands are not optimal prices; a "
            "position above the band is information, not automatically a problem."
        )
    if status == "in_band":
        return base + " It sits inside the reference band for its segment."
    if status == "insufficient_pricing_headroom":
        return (
            base + f" The economic floor (index {_idx(floor_idx)}) sits at or above the "
            "reference band, so the corridor between floor and band is infeasible. "
            "The engine flags this rather than relabeling the line."
        )
    return (
        "Not enough comparable observations exist in this pair's segment to place "
        "it against a reference band, so the engine reports insufficient data "
        "instead of a confident-looking position."
    )


def opportunity_explanation(row: dict[str, Any]) -> str:
    """Why this opportunity is sized the way it is, from its own numbers."""
    gross = row.get("gross_price_opportunity_cents")
    expected = row.get("expected_contribution_change_cents")
    treatment = row.get("volume_treatment") or "fixed_volume"
    status = row.get("evidence_status") or "insufficient_data"
    base = (
        f"Closing the gap to the proposed pocket price is worth {_dollars(gross)} "
        "over the baseline window at unchanged volume."
    )
    if treatment == "causal" and expected is not None:
        return (
            base + f" Because this segment's elasticity is causal-supported, the "
            f"expected contribution change after the volume response is "
            f"{_dollars(expected)}. The two figures answer different questions "
            "and are never added together."
        )
    if treatment == "iv_range":
        return (
            base + f" The segment elasticity is {status}, so the volume response is "
            "shown only as a labeled sensitivity range, not a point estimate."
        )
    return (
        base + f" The segment elasticity evidence is {status}, so no volume "
        "response is modeled: the figure assumes unchanged volume and is an "
        "upper-bound style gross number, not a forecast."
    )
