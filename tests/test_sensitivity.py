"""Phase 6 verification vs. section 8 targets.

Heavy by design: multi-seed recovery, 20-simulation CI coverage, and preset
behavior (weak / invalid instruments, price_constant, sparse). Worlds are
generated once per fixture and shared.
"""

from __future__ import annotations

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
    )
    return panel, truth, seg_map


# ---------------------------------------------------------------------------
# Multi-seed recovery studies: planted-segment grain (gated) and
# end-to-end on engine-recovered segments (gated separately below)
# ---------------------------------------------------------------------------

STUDY_SEEDS = [2, 8, 15, 21, 34]


@pytest.fixture(scope="module")
def study(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    """Recovery study at the planted-segment grain: the elasticity truth is
    defined per planted segment, and segmentation quality is gated separately
    (ARI targets in test_segmentation). Estimating on engine cells that
    fragment a planted segment would mix segmentation granularity noise into
    the elasticity target."""
    tmp = tmp_path_factory.mktemp("sens")
    rules = rules_full()
    results = []
    for seed in STUDY_SEEDS:
        panel, truth, seg_map = build_world_panel(
            tmp, Preset.WELL_IDENTIFIED, seed, use_engine_segments=False
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
        )
        for r in sens.to_dicts():
            planted = seg_map.get(r["segment"])
            if planted is None:
                continue
            r["seed"] = seed
            r["true_elasticity"] = truth[planted]
            results.append(r)
    return results


def test_elasticity_recovery_targets(study: list[dict[str, Any]]) -> None:
    errs = sorted(abs(r["iv"] - r["true_elasticity"]) for r in study if r["iv"] is not None)
    assert len(errs) >= 20
    median = float(np.median(errs))
    p90 = float(np.quantile(errs, 0.9))
    worst = max(errs)
    # Worst case is REPORTED per spec, not a pass/fail bound.
    print(
        f"\nelasticity recovery (planted grain): median={median:.3f} p90={p90:.3f} worst={worst:.3f}"
    )
    assert median <= 0.10, f"median abs error {median:.3f} > 0.10"
    assert p90 <= 0.15, f"P90 abs error {p90:.3f} > 0.15"


@pytest.fixture(scope="module")
def study_end_to_end(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    """End-to-end recovery: segmentation is the ENGINE's own (not planted),
    estimates per engine cell compared against the planted elasticity of the
    cell's modal planted segment (cells below 0.9 purity have no single truth
    and are excluded from the comparison; their estimates still exist)."""
    tmp = tmp_path_factory.mktemp("sens_e2e")
    rules = rules_full()
    results = []
    for seed in STUDY_SEEDS:
        panel, truth, seg_map = build_world_panel(
            tmp, Preset.WELL_IDENTIFIED, seed, use_engine_segments=True
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
        )
        for r in sens.to_dicts():
            planted = seg_map.get(r["segment"])
            if planted is None:
                continue
            r["seed"] = seed
            r["true_elasticity"] = truth[planted]
            results.append(r)
    return results


def test_end_to_end_elasticity_recovery(study_end_to_end: list[dict[str, Any]]) -> None:
    errs = sorted(
        abs(r["iv"] - r["true_elasticity"]) for r in study_end_to_end if r["iv"] is not None
    )
    assert len(errs) >= 20
    median = float(np.median(errs))
    p90 = float(np.quantile(errs, 0.9))
    worst = max(errs)
    print(
        f"\nelasticity recovery (end-to-end, engine segments): "
        f"median={median:.3f} p90={p90:.3f} worst={worst:.3f}"
    )
    assert median <= 0.10, f"end-to-end median abs error {median:.3f} > 0.10"
    assert p90 <= 0.15, f"end-to-end P90 abs error {p90:.3f} > 0.15"


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


def test_ci_coverage_50_simulations(tmp_path: Path) -> None:
    # Default generator scale; 50 simulations (300 segment estimates) per the
    # phase-17.1 review: enough resolution that passing means margin, not luck.
    rules = rules_full()
    covered, total = 0, 0
    for seed in range(100, 150):
        panel, truth, _ = build_world_panel(
            tmp_path, Preset.WELL_IDENTIFIED, seed, use_engine_segments=False
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
        )
        for r in sens.to_dicts():
            if r["iv_ci_low"] is None:
                continue
            t = truth[r["segment"]]
            total += 1
            covered += int(r["iv_ci_low"] - 1e-9 <= t <= r["iv_ci_high"] + 1e-9)
    rate = covered / total
    print(f"\n95% CI (cluster-jackknife) coverage over 50 sims: {covered}/{total} = {rate:.3f}")
    assert 0.90 <= rate <= 0.98, f"coverage {rate:.3f} outside [0.90, 0.98]"


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_weak_instrument_never_causal(tmp_path: Path) -> None:
    rules = rules_full()
    panel, _, _ = build_world_panel(tmp_path, Preset.WEAK_INSTRUMENT, 3, use_engine_segments=False)
    sens = run_sensitivity(
        panel,
        rules,
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
    )
    for r in sens.to_dicts():
        assert r["status"] != STATUS_CAUSAL, r
        # The weak-IV-robust (AR) fields are always part of the record: a wide
        # interval or an empty set (both bounds null) are valid reported
        # outcomes under weak identification.
        assert "ar_ci_low" in r and "ar_ci_high" in r


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
        )
        seg_rows = [r for r in sens.to_dicts() if r["overid_p"] is not None]
        if any(r["overid_p"] < 0.05 for r in seg_rows):
            rejected += 1
        if all(r["status"] != STATUS_CAUSAL for r in seg_rows if r["overid_p"] < 0.05):
            downgraded += 1
        n += 1
    assert rejected >= 0.8 * n, f"overid rejected on only {rejected}/{n} seeds"
    assert downgraded == n


def test_price_constant_never_above_descriptive(tmp_path: Path) -> None:
    rules = rules_full()
    panel, _, _ = build_world_panel(tmp_path, Preset.PRICE_CONSTANT, 3, use_engine_segments=False)
    sens = run_sensitivity(
        panel,
        rules,
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
    )
    for r in sens.to_dicts():
        assert r["status"] in (STATUS_INSUFFICIENT, STATUS_DESCRIPTIVE), r


def test_sparse_never_above_descriptive(tmp_path: Path) -> None:
    rules = rules_full()
    panel, _, _ = build_world_panel(tmp_path, Preset.SPARSE, 3, use_engine_segments=False)
    sens = run_sensitivity(
        panel,
        rules,
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
    )
    for r in sens.to_dicts():
        assert r["status"] in (STATUS_INSUFFICIENT, STATUS_DESCRIPTIVE), r


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
    assert zeros["price_carried_forward"].all()


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
        int(s): estimate_segment(panel.filter(pl.col("segment_leaf") == s), rules, int(s))
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


def test_realistic_noise_preset_informational(tmp_path: Path) -> None:
    """INFORMATIONAL, not gated: the realistic_noise preset uses Poisson count
    quantities (the planted beta is then only approximately the log-log
    estimand) and pre-tightening noise levels. Recovery metrics are printed and
    reported in the README; the assertions below check only that estimation
    runs and labels itself honestly."""
    rules = rules_full()
    errs: list[float] = []
    for seed in (2, 8, 15):
        panel, truth, seg_map = build_world_panel(
            tmp_path, Preset.REALISTIC_NOISE, seed, use_engine_segments=False
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
        )
        for r in sens.to_dicts():
            assert r["status"] in (
                STATUS_INSUFFICIENT,
                STATUS_DESCRIPTIVE,
                "iv_estimated_elasticity",
                STATUS_CAUSAL,
            )
            planted = seg_map.get(r["segment"])
            if planted is not None and r["iv"] is not None:
                errs.append(abs(r["iv"] - truth[planted]))
    assert len(errs) >= 10
    print(
        f"\nrealistic_noise recovery (informational): median={np.median(errs):.3f} "
        f"p90={np.quantile(errs, 0.9):.3f} worst={max(errs):.3f}"
    )
