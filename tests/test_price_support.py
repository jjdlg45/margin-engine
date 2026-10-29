"""Phase 74 (Run 6): the identification-support rule.

A volume forecast is only worth making where the instruments actually moved
price. The engine had an outer range for that - the 5th to 95th percentile of
instrument-predicted movement - and treated everything between the two ends as
equally supported. It is not. A segment whose cost shocks pushed price around
-6% in some months and +4% in others has told you nothing about what holding it
flat would do, and the old rule would have waved that through.

So the rule now works on REGIONS: stretches of movement dense enough to inform
a response. These tests cover the shapes that matter - one clean stretch, a
skewed one, two with a gap between them, and none at all - plus the distance
reporting that lets a reader see whether a refusal was marginal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from margin_engine.config import Rules, load_rules
from margin_engine.sensitivity.estimate import (
    identification_support,
    parse_support_regions,
    support_distance,
)

REPO = Path(__file__).resolve().parents[1]


def _profile(predicted: np.ndarray, **kw: Any):
    opts: dict[str, Any] = {
        "lo_quantile": 0.05,
        "hi_quantile": 0.95,
        "window": 0.02,
        "min_observations": 30,
    }
    opts.update(kw)
    return identification_support(predicted, predicted, **opts)


# ---------------------------------------------------------------------------
# The shapes support can take
# ---------------------------------------------------------------------------


def test_one_dense_stretch_is_one_region() -> None:
    rng = np.random.default_rng(1)
    prof = _profile(rng.normal(0, 0.05, 4000))
    assert len(prof.regions) == 1
    lo, hi = prof.regions[0]
    assert lo <= 0.0 <= hi
    assert prof.lo < lo + 1e-9 and hi - 1e-9 < prof.hi
    assert "separate stretches" not in prof.detail


def test_skewed_support_gives_an_off_centre_region() -> None:
    """Cost shocks that mostly pushed prices DOWN support downward moves and
    not upward ones, however symmetric a single min-max range would look."""
    rng = np.random.default_rng(2)
    predicted = -np.abs(rng.gamma(2.0, 0.02, 4000))
    prof = _profile(predicted)
    assert prof.hi < 0.01
    assert len(prof.regions) == 1
    lo, hi = prof.regions[0]
    # The supported stretch sits on the negative side, where the movement was.
    assert (lo + hi) / 2 < 0
    assert support_distance(-0.03, prof.regions) == 0.0
    assert support_distance(+0.05, prof.regions) > 0.0


def test_two_clusters_with_a_gap_are_two_regions_and_the_gap_is_refused() -> None:
    """The case the outer range gets wrong. Movement at about -6% and about
    +4%, nothing between: a proposed move of zero sits inside the outer bounds
    and is still an extrapolation."""
    rng = np.random.default_rng(3)
    predicted = np.concatenate([rng.normal(-0.06, 0.004, 2000), rng.normal(0.04, 0.004, 2000)])
    prof = _profile(predicted, window=0.005, min_observations=30)
    assert len(prof.regions) == 2, prof.regions
    assert "2 separate stretches" in prof.detail
    low, high = prof.regions
    assert low[1] < 0 < high[0]
    # Inside the outer bounds, outside both regions.
    assert prof.lo < 0.0 < prof.hi
    assert support_distance(0.0, prof.regions) > 0.0
    # And each cluster's own centre is supported.
    assert support_distance(-0.06, prof.regions) == 0.0
    assert support_distance(0.04, prof.regions) == 0.0


def test_too_little_movement_anywhere_leaves_no_region() -> None:
    rng = np.random.default_rng(4)
    prof = _profile(rng.normal(0, 0.05, 40), min_observations=30)
    assert prof.regions == ()
    assert "no part of" in prof.detail
    assert support_distance(0.0, prof.regions) == float("inf")


def test_no_movement_at_all_is_reported_rather_than_dividing_by_zero() -> None:
    prof = _profile(np.zeros(500))
    assert prof.regions == ()
    assert "no usable price movement" in prof.detail


# ---------------------------------------------------------------------------
# Distance to the boundary
# ---------------------------------------------------------------------------


def test_distance_is_zero_inside_and_grows_outside() -> None:
    regions = ((-0.05, -0.01), (0.02, 0.06))
    assert support_distance(-0.03, regions) == 0.0
    assert support_distance(0.04, regions) == 0.0
    assert support_distance(0.0, regions) == pytest.approx(0.01)  # nearer the upper one
    assert support_distance(-0.02, regions) == 0.0
    assert support_distance(0.10, regions) == pytest.approx(0.04)
    assert support_distance(-0.20, regions) == pytest.approx(0.15)


def test_the_window_and_the_minimum_count_both_move_the_regions() -> None:
    """They are configuration, and configuration that cannot change an outcome
    is decoration."""
    rng = np.random.default_rng(5)
    predicted = rng.normal(0, 0.05, 1500)
    wide = _profile(predicted, window=0.03, min_observations=30)
    narrow = _profile(predicted, window=0.002, min_observations=30)
    strict = _profile(predicted, window=0.03, min_observations=5000)
    wide_span = sum(b - a for a, b in wide.regions)
    narrow_span = sum(b - a for a, b in narrow.regions)
    assert wide_span > narrow_span
    assert strict.regions == ()


# ---------------------------------------------------------------------------
# The compact export form
# ---------------------------------------------------------------------------


def test_regions_survive_a_round_trip_through_the_export_form() -> None:
    rng = np.random.default_rng(6)
    prof = _profile(
        np.concatenate([rng.normal(-0.06, 0.004, 2000), rng.normal(0.04, 0.004, 2000)]),
        window=0.005,
    )
    back = parse_support_regions(prof.regions_text)
    assert len(back) == len(prof.regions)
    for (a, b), (c, d) in zip(prof.regions, back, strict=True):
        assert a == pytest.approx(c, abs=1e-6)
        assert b == pytest.approx(d, abs=1e-6)


def test_an_empty_export_form_reads_back_as_no_regions() -> None:
    assert parse_support_regions("") == ()


# ---------------------------------------------------------------------------
# The rule as the opportunity module applies it
# ---------------------------------------------------------------------------


def _rules() -> Rules:
    return load_rules(REPO / "config" / "rules.yaml")


def _sensitivity(*, regions: str | None, lo: float = -0.10, hi: float = 0.10) -> pl.DataFrame:
    row: dict[str, Any] = {
        "segment": 1,
        "iv": -1.5,
        "status": "causal_elasticity_supported",
        "iv_ci_low": -1.7,
        "iv_ci_high": -1.3,
        "support_log_lo": lo,
        "support_log_hi": hi,
        "competitor_path_resolution": "controlled_by_market_index",
    }
    if regions is not None:
        row["support_regions"] = regions
    return pl.DataFrame([row])


def _sized(sens: pl.DataFrame, target_index: float = 0.95):
    """One pair, one month, priced at 0.90 of list against the given target,
    sized under the shipped rules."""
    from margin_engine.opportunity.sizing import price_opportunities

    panel = pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
                "segment_leaf": 1,
                "month_idx": 24300,
                "qty": 20.0,
                "price_cf": 9000.0,
                "supplier_id": "S1",
                "item_category": "CATA",
                "contract_id": None,
            }
        ]
    ).with_columns(pl.col("contract_id").cast(pl.String))
    guidance = pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
                "segment_leaf": 1,
                "guidance_status": "below_reference",
                "target_index": target_index,
                "current_index": 0.90,
                "eff_list_cents": 10_000,
                "unit_cost_cents": 6_000,
                "invoice_floor_cents": 6_316,
                "band_p25": 0.92,
                "band_p75": 0.97,
                "contract_protected": False,
                "deduction_bp": 0,
                "fixed_deduction_cents": 0,
            }
        ]
    )
    return price_opportunities(guidance, panel, sens, Rules()).to_dicts()[0]


def test_a_frame_without_regions_is_held_to_the_outer_bounds() -> None:
    """Backwards compatibility, stated as a test rather than assumed: a
    sensitivity frame written before the regions existed keeps exactly the
    behaviour it had, judged on the outer bounds alone."""
    inside = _sized(_sensitivity(regions=None, lo=-0.5, hi=0.5))
    assert inside["volume_treatment"] == "expected_change"
    assert inside["price_support_reason"] == "inside the movement the instruments induced"
    assert inside["price_support_distance"] == pytest.approx(0.0)

    outside = _sized(_sensitivity(regions=None, lo=-0.001, hi=0.001))
    assert outside["volume_treatment"] == "outside_price_support"
    assert outside["price_support_distance"] > 0


def test_a_move_in_the_gap_between_two_regions_is_refused() -> None:
    """The case the outer bounds get wrong, end to end. The proposal is a
    +5.4% move; the segment's instruments moved price around -6% and around
    +10%, so the outer bounds contain the proposal and nothing near it does."""
    sens = _sensitivity(regions="-0.070000:-0.050000|0.090000:0.110000", lo=-0.08, hi=0.12)
    row = _sized(sens)
    assert row["volume_treatment"] == "outside_price_support"
    # log(0.95/0.90) = 0.0541; nearest region starts at 0.09.
    assert row["price_support_distance"] == pytest.approx(0.09 - 0.05407, abs=1e-3)
    assert row["price_support_reason"] == "beyond the movement the instruments induced"
    # Same bounds, no gap: the same move is fine.
    assert (
        _sized(_sensitivity(regions="-0.080000:0.120000", lo=-0.08, hi=0.12))["volume_treatment"]
        == "expected_change"
    )


def test_a_measured_segment_with_no_dense_movement_refuses_every_move() -> None:
    """An empty regions string is a measurement, not a missing value: it says
    the instruments moved price nowhere densely enough to use. That has to
    refuse, where a genuinely absent measurement does not."""
    assert parse_support_regions("") == ()
    assert support_distance(0.01, parse_support_regions("")) == float("inf")
    row = _sized(_sensitivity(regions="", lo=-0.5, hi=0.5))
    assert row["volume_treatment"] == "outside_price_support"


def test_a_segment_never_measured_against_the_rule_is_not_refused_by_it() -> None:
    """No instruments means no support record, and a rule nobody measured
    cannot be the reason a record is held back; the evidence status is."""
    sens = _sensitivity(regions=None, lo=-0.5, hi=0.5).drop("support_log_lo", "support_log_hi")
    row = _sized(sens)
    assert row["price_support_reason"] == "not measured for this segment"
    assert row["price_support_distance"] is None


def test_the_profile_reports_the_observed_spread_beside_the_instrumented_one() -> None:
    """The point of showing both: if the instruments account for a sliver of
    the price movement in the data, a reader should be able to see that."""
    rng = np.random.default_rng(7)
    predicted = rng.normal(0, 0.01, 3000)
    observed = rng.normal(0, 0.08, 3000)
    prof = identification_support(
        predicted,
        observed,
        lo_quantile=0.05,
        hi_quantile=0.95,
        window=0.02,
        min_observations=30,
    )
    assert prof.observed_hi - prof.observed_lo > 4 * (prof.hi - prof.lo)
    assert "Observed price movement" in prof.detail


# ---------------------------------------------------------------------------
# A tie that used to flip between runs
# ---------------------------------------------------------------------------


def test_the_price_source_mix_breaks_ties_by_name() -> None:
    """Found by the scenario matrix while this phase was being checked, and
    not caused by it: the mix string sorted on the row count alone, so two
    categories with equal counts came out in whichever order the group-by
    hash table produced. On a world where observed and carried-forward prices
    each took 49% of the rows, the exported parquet differed between identical
    runs about six times in ten."""
    from margin_engine.sensitivity.estimate import price_source_mix

    tied = pl.DataFrame(
        {"price_status": ["observed"] * 10 + ["carried_forward"] * 10 + ["quoted"] * 2}
    )
    first = price_source_mix(tied)
    # Equal counts, so the name decides and the answer is the same every time.
    assert first.startswith("carried forward 45%; observed 45%")
    assert price_source_mix(tied.sample(fraction=1.0, shuffle=True, seed=7)) == first
    assert price_source_mix(tied.reverse()) == first
    # A clear winner still leads, tie or no tie.
    lopsided = pl.DataFrame({"price_status": ["observed"] * 3 + ["carried_forward"]})
    assert lopsided.pipe(price_source_mix).startswith("observed 75%")
    assert (
        price_source_mix(pl.DataFrame({"price_status": []}, schema={"price_status": pl.String}))
        == ""
    )
