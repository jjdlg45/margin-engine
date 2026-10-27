"""Phase 6 verification vs. section 8 targets.

**Two tiers (Run 6, Phase 72).** Every check here exists at both scales:

* the DEFAULT tier runs small worlds and few seeds, so a commit trips a
  regression in minutes and inside a bounded memory budget;
* the SLOW tier (`-m slow`) runs the same checks at the scale the section 8
  targets are stated for, with the stated thresholds.

Nothing was loosened to make the default tier pass. The section 8 numbers of
record are produced by `scripts/measure_validation.py`, which runs the full
study independently and writes `docs/validation.json`; the slow-tier copies
below are the same measurement inside pytest. The default-tier thresholds are
deliberately wider and are labeled where they appear, because a world with a
third of the customers carries a third of the identifying variation and would
otherwise fail for a reason that has nothing to do with the estimator.

The split exists because the full tier does not fit this project's development
machine: the 50-simulation coverage study alone generates 50 full-size worlds.
"""

from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.panel.build import build_panel
from margin_engine.segment.trees import fit_commercial_segments, prepare_segmentation_frame
from margin_engine.sensitivity.estimate import (
    STATUS_CAUSAL,
    STATUS_DESCRIPTIVE,
    STATUS_INSUFFICIENT,
    estimate_segment,
    run_sensitivity,
)
from margin_engine.sensitivity.market_control import SOURCE_MAPPED, select_market_index
from margin_engine.sensitivity.shrinkage import shrink_to_segment

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def rules_full() -> Rules:
    return load_rules(REPO / "config" / "rules.yaml")


def build_world_panel(
    tmp: Path,
    preset: Preset,
    seed: int,
    *,
    use_engine_segments: bool,
    size: dict[str, Any] | None = None,
) -> tuple[pl.DataFrame, dict[int, float], dict[int, int]]:
    """Returns (panel, planted elasticity by planted segment, engine->planted map)."""
    rules = rules_full()
    cfg = GeneratorConfig.for_preset(preset, seed=seed, **(size or {}))
    out = tmp / f"{preset.value}_{seed}"
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, rules)
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    pt = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    planted_pairs = pt.select("customer_id", "item_id", "segment_id").unique()
    truth = {
        s["segment_id"]: s["elasticity"]
        for s in json.loads((out / "ground_truth" / "truth_summary.json").read_text())["segments"]
    }
    seg_map: dict[int, int] = {}
    if use_engine_segments:
        fast = Rules.model_validate({"segmentation": {"bootstrap_resamples": 2}})
        df = prepare_segmentation_frame(flagged, res.reference["customers"], res.reference["items"])
        seg = fit_commercial_segments(df, fast, seed=1)
        obs = flagged.join(seg.leaf_assignments, on="source_line_key", how="left")
        # Map each engine cell to its modal planted segment for truth lookup.
        # Cells mixing planted segments (purity < 0.9) have no single true
        # elasticity, so they are excluded from the truth comparison (their
        # estimates are still produced and reported like any other cell).
        modal = (
            obs.join(planted_pairs, on=["customer_id", "item_id"], how="left")
            .group_by("segment_leaf")
            .agg(
                pl.col("segment_id").mode().first().alias("planted"),
                (pl.col("segment_id") == pl.col("segment_id").mode().first())
                .mean()
                .alias("purity"),
            )
            .drop_nulls()
            .filter(pl.col("purity") >= 0.90)
        )
        seg_map = {int(r["segment_leaf"]): int(r["planted"]) for r in modal.to_dicts()}
    else:
        obs = flagged.join(planted_pairs, on=["customer_id", "item_id"], how="left").with_columns(
            pl.col("segment_id").alias("segment_leaf")
        )
        seg_map = {s: s for s in truth}
    panel = build_panel(
        obs,
        res.reference["items"],
        res.reference["supplier_cost_index"],
        res.reference["tariff_schedule"],
        rules_full(),
        # The published market price index goes through the SAME public-source
        # decision production uses (Run 6, Phase 70), so these worlds exercise
        # the competitor-price gate instead of bypassing it.
        market_price_index=select_market_index(
            res.reference.get("market_price_index"),
            public_source=res.mapping.reference_tables["market_price_index"].public_source
            if "market_price_index" in res.mapping.reference_tables
            else None,
            rules=rules,
        )[0],
        # Zero-row price sources, exactly as the engine passes them (Run 6,
        # Phase 72). Without these the study would measure a construction the
        # engine no longer ships: every zero row carried forward.
        quotes=res.reference.get("quotes"),
        price_list=res.reference.get("price_list"),
    )
    return panel, truth, seg_map


# ---------------------------------------------------------------------------
# Multi-seed recovery studies: planted-segment grain (gated) and
# end-to-end on engine-recovered segments (gated separately below)
# ---------------------------------------------------------------------------

STUDY_SEEDS = [2, 8, 15, 21, 34]
# Default tier: a third of the customers and items, three seeds. Measured peak
# memory for a world at this scale is about 1.0 GB against about 2.8 GB at full
# scale, and it generates in roughly a quarter of the time.
SMALL = {"n_customers": 120, "n_items": 200}
# The end-to-end study puts the engine's OWN segmentation in the loop, and only
# cells that are 90% one planted segment have a single truth to be scored
# against. A small world fragments segmentation enough that three seeds yielded
# three such cells - too few to take a median of - so that study runs one size
# up. Measured: 3 pure cells at SMALL, 13 at this size.
MEDIUM = {"n_customers": 250, "n_items": 400}
FAST_SEEDS = [2, 8, 15]


def _recovery_study(
    tmp: Path,
    seeds: list[int],
    *,
    use_engine_segments: bool,
    size: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """One world per seed, estimated, annotated with the planted truth.

    Each world's panel is released before the next is built. The loop used to
    drop its panel by accident, through rebinding; it does so on purpose now,
    because the worlds are the memory cost here and several of them alive at
    once is the difference between fitting on a developer machine and swapping.
    """
    rules = rules_full()
    results: list[dict[str, Any]] = []
    for seed in seeds:
        panel, truth, seg_map = build_world_panel(
            tmp, Preset.WELL_IDENTIFIED, seed, use_engine_segments=use_engine_segments, size=size
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
            # The recovery target is the CONDITIONAL estimate's. The
            # total-quantity estimand has its own study in
            # tests/test_total_quantity.py, and fitting a second Poisson per
            # segment per seed here would be work whose result is discarded.
            with_total_quantity=False,
        )
        for r in sens.to_dicts():
            planted = seg_map.get(r["segment"])
            if planted is None:
                continue
            r["seed"] = seed
            r["true_elasticity"] = truth[planted]
            results.append(r)
        del panel, sens
        gc.collect()
    return results


def _assert_recovery(
    study: list[dict[str, Any]],
    *,
    label: str,
    min_cells: int,
    median_max: float,
    p90_max: float,
) -> None:
    errs = sorted(abs(r["iv"] - r["true_elasticity"]) for r in study if r["iv"] is not None)
    assert len(errs) >= min_cells, f"{label}: only {len(errs)} estimated cells"
    median = float(np.median(errs))
    p90 = float(np.quantile(errs, 0.9))
    worst = max(errs)
    # Worst case is REPORTED per spec, not a pass/fail bound.
    print(f"\nelasticity recovery ({label}): median={median:.3f} p90={p90:.3f} worst={worst:.3f}")
    assert median <= median_max, f"{label}: median abs error {median:.3f} > {median_max}"
    assert p90 <= p90_max, f"{label}: P90 abs error {p90:.3f} > {p90_max}"


@pytest.fixture(scope="module")
def study(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    """Recovery study at the planted-segment grain: the elasticity truth is
    defined per planted segment, and segmentation quality is gated separately
    (ARI targets in test_segmentation). Estimating on engine cells that
    fragment a planted segment would mix segmentation granularity noise into
    the elasticity target.

    Default tier, so small worlds and three seeds. The qualitative tests that
    read this fixture - naive bias direction, status reachability, diagnostic
    completeness - do not depend on scale. The section 8 error bounds do, and
    are asserted at full scale in the slow tier.
    """
    return _recovery_study(
        tmp_path_factory.mktemp("sens"), FAST_SEEDS, use_engine_segments=False, size=SMALL
    )


def test_elasticity_recovery_smoke(study: list[dict[str, Any]]) -> None:
    """Default tier. This bound is WIDER than the section 8 target and is not
    it: these worlds carry a third of the identifying variation, so the
    estimator's sampling error is legitimately larger here. The section 8
    target is asserted at full scale by `test_elasticity_recovery_targets`
    below, and measured independently by scripts/measure_validation.py.
    """
    _assert_recovery(
        study, label="planted grain, small worlds", min_cells=10, median_max=0.20, p90_max=0.35
    )


@pytest.mark.slow
def test_elasticity_recovery_targets(tmp_path: Path) -> None:
    """Section 8: median absolute error <= 0.10, P90 <= 0.15, over five seeds
    at the generator's default scale."""
    _assert_recovery(
        _recovery_study(tmp_path, STUDY_SEEDS, use_engine_segments=False, size=None),
        label="planted grain",
        min_cells=20,
        median_max=0.10,
        p90_max=0.15,
    )


@pytest.fixture(scope="module")
def study_end_to_end(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    """End-to-end recovery: segmentation is the ENGINE's own (not planted),
    estimates per engine cell compared against the planted elasticity of the
    cell's modal planted segment (cells below 0.9 purity have no single truth
    and are excluded from the comparison; their estimates still exist).

    Default tier, so small worlds and three seeds; the section 8 bounds are
    asserted at full scale in the slow tier below."""
    return _recovery_study(
        tmp_path_factory.mktemp("sens_e2e"), FAST_SEEDS, use_engine_segments=True, size=MEDIUM
    )


def test_end_to_end_recovery_smoke(study_end_to_end: list[dict[str, Any]]) -> None:
    """Default tier; wider than the section 8 target, and labeled as such. Here
    the engine's own segmentation is in the loop as well, and a small world
    fragments it further, so this bound is wider again than the planted-grain
    smoke bound."""
    _assert_recovery(
        study_end_to_end,
        label="end-to-end, reduced worlds",
        min_cells=8,
        median_max=0.25,
        p90_max=0.45,
    )


@pytest.mark.slow
def test_end_to_end_elasticity_recovery(tmp_path: Path) -> None:
    """Section 8, end to end: the engine's own segments, five seeds, default
    generator scale."""
    _assert_recovery(
        _recovery_study(tmp_path, STUDY_SEEDS, use_engine_segments=True, size=None),
        label="end-to-end, engine segments",
        min_cells=20,
        median_max=0.10,
        p90_max=0.15,
    )


def test_naive_biased_toward_zero_every_seed(study: list[dict[str, Any]]) -> None:
    by_seed: dict[int, list[bool]] = {}
    for r in study:
        if r["naive_fe_ols"] is None:
            continue
        by_seed.setdefault(r["seed"], []).append(r["naive_fe_ols"] > r["true_elasticity"])
    assert by_seed
    for seed, flags in by_seed.items():
        assert all(flags), f"seed {seed}: naive estimate not biased toward zero everywhere"


def test_causal_status_reachable_and_criteria_enforced(
    study: list[dict[str, Any]],
) -> None:
    statuses = {r["status"] for r in study}
    assert STATUS_CAUSAL in statuses
    for r in study:
        if r["status"] == STATUS_CAUSAL:
            assert r["first_stage_f"] >= 10
            assert r["overid_p"] is None or r["overid_p"] >= 0.05
            assert r["robustness_spread"] <= 0.20
            assert (r["iv_ci_high"] - r["iv_ci_low"]) <= 1.0


def test_diagnostics_reported(study: list[dict[str, Any]]) -> None:
    # Full diagnostics are mandatory wherever an IV-based status is claimed;
    # thin-support cells capped at descriptive carry their reason instead.
    for r in study:
        if r["status"] in ("iv_estimated_elasticity", "causal_elasticity_supported"):
            assert r["first_stage_f"] is not None
            assert r["iv_ci_low"] is not None and r["iv_ci_high"] is not None
            assert r["identifying_coverage"] is not None
            # Overidentification and robustness are reported against the
            # instruments that still IDENTIFY price after the market control
            # (Run 6, Phase 70). Holding the category market price constant
            # removes nearly all of a category-level tariff instrument's
            # variation, which leaves the model just-identified: the test is
            # then reported as not evaluated, and the instrument count says
            # so. A missing figure must therefore be accompanied by its
            # reason, never left blank.
            assert r["n_identified_instruments"] is not None
            if int(r["n_identified_instruments"]) >= 2:
                assert r["overid_p"] is not None
            else:
                assert r["overid_p"] is None
                assert r["robustness_excluded_weak"] or r["status_reasons"]
            if r["robustness_spread"] is None:
                # No identified alternative exists, so the spread is unknown -
                # which must cap the status, not pass silently.
                assert r["status"] == "iv_estimated_elasticity"
                assert "robustness could not be assessed" in r["status_reasons"]
            # The estimand is always named, and a causal status requires the
            # competitor-price path to be addressed.
            assert r["estimand"] in (
                "unilateral_own_price_response",
                "market_wide_cost_shock_response",
            )
            if r["status"] == "causal_elasticity_supported":
                assert r["estimand"] == "unilateral_own_price_response"
                assert r["competitor_path_resolution"] in (
                    "controlled_by_market_index",
                    "no_competitor_price_path",
                )
            # AR fields exist as the weak-IV-robust companion (an empty set,
            # both bounds null, is itself a reported outcome).
            assert "ar_ci_low" in r and "ar_ci_high" in r
        else:
            assert r["status_reasons"]


# ---------------------------------------------------------------------------
# CI coverage: 20 simulations (planted segments; estimator property)
# ---------------------------------------------------------------------------


def _coverage(tmp: Path, seeds: range, size: dict[str, Any] | None) -> tuple[int, int]:
    """Interval coverage over a run of simulated worlds.

    One world at a time, each released before the next: this is the longest
    loop in the suite and the one that decides its memory ceiling.
    """
    rules = rules_full()
    covered, total = 0, 0
    for seed in seeds:
        panel, truth, _ = build_world_panel(
            tmp, Preset.WELL_IDENTIFIED, seed, use_engine_segments=False, size=size
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
            # This study measures the CONDITIONAL estimator's interval
            # coverage and never reads the total-quantity estimate, so fitting
            # a second Poisson per segment per simulation would be discarded
            # work. With it in the loop this study alone took 3,518s.
            with_total_quantity=False,
        )
        for r in sens.to_dicts():
            if r["iv_ci_low"] is None:
                continue
            t = truth[r["segment"]]
            total += 1
            covered += int(r["iv_ci_low"] - 1e-9 <= t <= r["iv_ci_high"] + 1e-9)
        del panel, sens
        gc.collect()
    return covered, total


def test_ci_coverage_smoke(tmp_path: Path) -> None:
    """Default tier: 16 small worlds. The band is WIDER than the section 8
    target of 90-98% and is not it - roughly 70 intervals cannot resolve a
    two-point-wide band, and asserting one here would fail on sampling noise
    alone. What this catches is the failure that matters between commits: an
    interval that has stopped covering anything, or one so wide it covers
    everything. The section 8 figure is measured over 50 full-size worlds by
    the slow-tier test below and by scripts/measure_validation.py.

    Sixteen worlds rather than the twelve this started with. Run 6 Phase 73
    added the cluster-adequacy criterion, which declines to report an interval
    for a segment with too few independent groups - and on worlds this small
    some segments have only nine or ten distinct customers, so a given number
    of worlds now yields fewer intervals (37 from twelve, against the 40 this
    needs). The sample-size floor below is what keeps the rate meaningful, so
    it was left where it was and the worlds were added instead."""
    covered, total = _coverage(tmp_path, range(200, 216), SMALL)
    rate = covered / total
    print(f"\n95% CI coverage over 12 small worlds: {covered}/{total} = {rate:.3f}")
    assert total >= 40, f"only {total} intervals produced"
    assert 0.80 <= rate <= 1.0, f"coverage {rate:.3f} outside the smoke band [0.80, 1.00]"


@pytest.mark.slow
def test_ci_coverage_50_simulations(tmp_path: Path) -> None:
    # Default generator scale; 50 simulations (300 segment estimates) per the
    # phase-17.1 review: enough resolution that passing means margin, not luck.
    covered, total = _coverage(tmp_path, range(100, 150), None)
    rate = covered / total
    print(f"\n95% CI (cluster-jackknife) coverage over 50 sims: {covered}/{total} = {rate:.3f}")
    assert 0.90 <= rate <= 0.98, f"coverage {rate:.3f} outside [0.90, 0.98]"


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def _preset_statuses(
    tmp: Path, preset: Preset, seed: int, size: dict[str, Any] | None
) -> list[dict[str, Any]]:
    panel, _truth, _map = build_world_panel(tmp, preset, seed, use_engine_segments=False, size=size)
    sens = run_sensitivity(
        panel,
        rules_full(),
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
        # Preset behaviour here is about the CONDITIONAL ladder's statuses.
        # The total-quantity estimand is exercised in
        # tests/test_total_quantity.py; fitting it, its split-panel halves and
        # its two zero-row robustness refits per segment would multiply this
        # file's runtime for a number no assertion here reads.
        with_total_quantity=False,
    )
    rows = sens.to_dicts()
    del panel, sens
    gc.collect()
    return rows


def _assert_never_causal(rows: list[dict[str, Any]], label: str) -> None:
    assert rows, f"{label}: the preset produced no segment at all"
    for r in rows:
        assert r["status"] != STATUS_CAUSAL, r
        # The weak-IV-robust (AR) fields are always part of the record: a wide
        # interval or an empty set (both bounds null) are valid reported
        # outcomes under weak identification.
        assert "ar_ci_low" in r and "ar_ci_high" in r


def _assert_never_above_descriptive(rows: list[dict[str, Any]], label: str) -> None:
    assert rows, f"{label}: the preset produced no segment at all"
    for r in rows:
        assert r["status"] in (STATUS_INSUFFICIENT, STATUS_DESCRIPTIVE), r


# A fail-closed assertion gets EASIER on a smaller world, because less data is
# less chance to clear a bar by accident. So these run at reduced scale on
# every commit and at the generator's default scale in the slow tier - the
# reduced run is the tripwire, the full run is the claim. Measured: a
# default-scale world costs about 2.3 GB of peak process memory on top of
# whatever the session already holds, which is what put this file over its
# budget on a 14 GB machine.


def test_weak_instrument_never_causal_smoke(tmp_path: Path) -> None:
    _assert_never_causal(
        _preset_statuses(tmp_path, Preset.WEAK_INSTRUMENT, 3, MEDIUM), "weak instrument, reduced"
    )


@pytest.mark.slow
def test_weak_instrument_never_causal(tmp_path: Path) -> None:
    _assert_never_causal(
        _preset_statuses(tmp_path, Preset.WEAK_INSTRUMENT, 3, None), "weak instrument"
    )


def test_invalid_instrument_overid_rejects(tmp_path: Path) -> None:
    rules = rules_full()
    rejected, downgraded, n = 0, 0, 0
    for seed in (3, 11, 19, 27, 35):
        panel, _, _ = build_world_panel(
            tmp_path,
            Preset.INVALID_INSTRUMENT,
            seed,
            use_engine_segments=False,
            size={"n_customers": 250, "n_items": 400},
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
            # Conditional ladder only; see the note above.
            with_total_quantity=False,
        )
        seg_rows = [r for r in sens.to_dicts() if r["overid_p"] is not None]
        if any(r["overid_p"] < 0.05 for r in seg_rows):
            rejected += 1
        if all(r["status"] != STATUS_CAUSAL for r in seg_rows if r["overid_p"] < 0.05):
            downgraded += 1
        n += 1
    assert rejected >= 0.8 * n, f"overid rejected on only {rejected}/{n} seeds"
    assert downgraded == n


def test_price_constant_never_above_descriptive_smoke(tmp_path: Path) -> None:
    _assert_never_above_descriptive(
        _preset_statuses(tmp_path, Preset.PRICE_CONSTANT, 3, MEDIUM), "price constant, reduced"
    )


@pytest.mark.slow
def test_price_constant_never_above_descriptive(tmp_path: Path) -> None:
    _assert_never_above_descriptive(
        _preset_statuses(tmp_path, Preset.PRICE_CONSTANT, 3, None), "price constant"
    )


def test_sparse_never_above_descriptive(tmp_path: Path) -> None:
    # The sparse preset is 40 customers by construction, so it has only one
    # scale and needs no second tier.
    _assert_never_above_descriptive(_preset_statuses(tmp_path, Preset.SPARSE, 3, None), "sparse")


# ---------------------------------------------------------------------------
# Panel and shrinkage
# ---------------------------------------------------------------------------


def test_panel_zero_rows_only_when_active_and_available(tmp_path: Path) -> None:
    panel, _, _ = build_world_panel(
        tmp_path,
        Preset.WELL_IDENTIFIED,
        4,
        use_engine_segments=False,
        size={"n_customers": 120, "n_items": 250},
    )
    zeros = panel.filter(pl.col("qty") == 0)
    assert zeros.height > 0
    assert zeros["active"].all()
    assert zeros["item_available"].all()
    # Every zero row carries a price from a DECLARED rung of the ladder, never
    # an unlabeled one (Run 6, Phase 72). This replaces the older assertion
    # that every zero row was carried forward, which was true only while that
    # was the single available source; the harness now passes the quotes and
    # price-list tables the engine passes, so better rungs apply.
    assert zeros["price_cf"].is_not_null().all()
    assert set(zeros["price_status"].unique().to_list()) <= {
        "quoted",
        "list_x_pair_discount",
        "carried_forward",
    }
    # And a purchase is never labeled as an imputation.
    bought = panel.filter(pl.col("qty") > 0)
    assert bought["price_status"].unique().to_list() == ["observed"]
    assert not bought["price_carried_forward"].any()
    # The flag keeps meaning what it says: it marks the last-resort rung only.
    assert (zeros["price_carried_forward"] == (zeros["price_status"] == "carried_forward")).all()


def test_shrinkage_reports_raw_and_shrunk(tmp_path: Path) -> None:
    rules = rules_full()
    panel, _, _ = build_world_panel(
        tmp_path,
        Preset.WELL_IDENTIFIED,
        4,
        use_engine_segments=False,
        size={"n_customers": 120, "n_items": 250},
    )
    segs = [s for s in panel["segment_leaf"].unique().to_list() if s is not None]
    parents = {
        int(s): estimate_segment(
            panel.filter(pl.col("segment_leaf") == s),
            rules,
            int(s),
            # Shrinkage pools the CONDITIONAL estimate; the parent fits here
            # exist only to supply it.
            with_total_quantity=False,
        )
        for s in segs
    }
    shrunk = shrink_to_segment(panel, parents, rules)
    assert shrunk.height > 0
    est = shrunk.filter(pl.col("raw_elasticity").is_not_null())
    assert est.height > 0
    # Shrunk lies between raw and parent.
    between = est.filter(
        (
            (pl.col("shrunk_elasticity") - pl.col("raw_elasticity"))
            * (pl.col("shrunk_elasticity") - pl.col("parent_elasticity"))
        )
        <= 1e-9
    )
    assert between.height == est.height
    assert (est["effective_sample_size"] > 0).all()


def _realistic_noise(
    tmp: Path, seeds: tuple[int, ...], size: dict[str, Any] | None
) -> tuple[list[float], int]:
    rules = rules_full()
    errs: list[float] = []
    n_segments = 0
    for seed in seeds:
        panel, truth, seg_map = build_world_panel(
            tmp, Preset.REALISTIC_NOISE, seed, use_engine_segments=False, size=size
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
            # Conditional ladder only; see the note above.
            with_total_quantity=False,
        )
        for r in sens.to_dicts():
            n_segments += 1
            assert r["status"] in (
                STATUS_INSUFFICIENT,
                STATUS_DESCRIPTIVE,
                "iv_estimated_elasticity",
                STATUS_CAUSAL,
            )
            planted = seg_map.get(r["segment"])
            if planted is not None and r["iv"] is not None:
                errs.append(abs(r["iv"] - truth[planted]))
        del panel, sens
        gc.collect()
    return errs, n_segments


def test_realistic_noise_labels_itself_honestly(tmp_path: Path) -> None:
    """Default tier: the honesty assertions, on one small world. Every status
    the preset produces must be one the engine knows how to describe. The
    recovery METRICS this preset exists to report are informational and are
    measured at full scale in the slow tier below, because they are read off a
    deliberate run rather than gated on every commit."""
    _errs, n_segments = _realistic_noise(tmp_path, (2,), SMALL)
    # The status assertions live inside the helper and only bite on segments it
    # actually estimated, so the count is what makes this test say anything.
    assert n_segments > 0, "the preset produced no segment to label"


@pytest.mark.slow
def test_realistic_noise_preset_informational(tmp_path: Path) -> None:
    """INFORMATIONAL, not gated: the realistic_noise preset uses Poisson count
    quantities (the planted beta is then only approximately the log-log
    estimand) and pre-tightening noise levels. Recovery metrics are printed and
    reported in the README; the assertions check only that estimation runs and
    labels itself honestly."""
    errs, _n = _realistic_noise(tmp_path, (2, 8, 15), None)
    assert len(errs) >= 10
    print(
        f"\nrealistic_noise recovery (informational): median={np.median(errs):.3f} "
        f"p90={np.quantile(errs, 0.9):.3f} worst={max(errs):.3f}"
    )
