"""Measure every validation claim and write docs/validation.json.

One source of truth, no hand-typed numbers: numeric studies reuse the test
suite's own measurement helpers (tests.test_sensitivity.build_world_panel and
the same seeds/scales the gated tests use), and fixture-exactness claims are
measured by running their pytest files and recording the outcome. The web
bundle reads docs/validation.json verbatim.

Full run takes on the order of two hours (the 50-simulation coverage study
dominates). Usage:

    uv run python scripts/measure_validation.py            # full
    uv run python scripts/measure_validation.py --quick    # skip coverage+perf (smoke only)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from synth.config import GeneratorConfig, Preset  # noqa: E402
from synth.generate import generate  # noqa: E402
from tests.test_sensitivity import STUDY_SEEDS, build_world_panel, rules_full  # noqa: E402

from margin_engine.config import Rules, load_rules  # noqa: E402
from margin_engine.dispersion.analytics import run_dispersion  # noqa: E402
from margin_engine.extended.kvi import kvi_candidate_scores  # noqa: E402
from margin_engine.extended.quotes import STATUS_ESTIMATED, fit_win_curves, quote_price_index  # noqa: E402
from margin_engine.ledger.pipeline import run_pipeline  # noqa: E402
from margin_engine.segment.trees import fit_commercial_segments, prepare_segmentation_frame  # noqa: E402
from margin_engine.sensitivity.estimate import (  # noqa: E402
    CLUSTER_LEVEL_NAMES,
    STATUS_CAUSAL,
    STATUS_DESCRIPTIVE,
    STATUS_INSUFFICIENT,
    run_sensitivity,
)
from margin_engine.sensitivity.market_control import SOURCE_MAPPED  # noqa: E402

MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def _row(check: str, target: str, achieved: str, passed: bool) -> dict[str, Any]:
    print(f"  [{'PASS' if passed else 'FAIL'}] {check}: {achieved}")
    return {"check": check, "target": target, "achieved": achieved, "passed": bool(passed)}


def _pytest_pass(node: str) -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-header"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def fixture_checks() -> list[dict[str, Any]]:
    print("fixture-exactness checks (pytest)...")
    rows = []
    ok = _pytest_pass("tests/test_waterfall_fixtures.py")
    rows.append(
        _row(
            "Accounting fixtures",
            "Exact to the cent",
            "All hand-calculated fixture tests pass" if ok else "Fixture tests FAILED",
            ok,
        )
    )
    ok = _pytest_pass("tests/property/test_reconciliation.py")
    rows.append(
        _row(
            "Ledger reconciliation",
            "Exact to source totals after rounding",
            "Property suite passes (hypothesis-varied inputs)" if ok else "FAILED",
            ok,
        )
    )
    ok = _pytest_pass("tests/test_recovery.py")
    rows.append(
        _row(
            "Rebate / freight recovery",
            "Actual amounts exact; allocated within ±5% of planted",
            "Recovery suite passes (exact + ±5% allocated)" if ok else "FAILED",
            ok,
        )
    )
    return rows


def outlier_check(tmp: Path) -> dict[str, Any]:
    print("planted outlier detection (seed 5, 100x300)...")
    out = tmp / "disp"
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=5, n_customers=100, n_items=300)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, Rules())
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], Rules())
    truth = pl.read_parquet(out / "ground_truth" / "line_truth.parquet")
    j = flagged.join(
        truth.select("line_key", "truth_outlier_type"),
        left_on="source_line_key",
        right_on="line_key",
        how="left",
    )
    any_flag = pl.col("flag_data_quality_anomaly") | pl.col("flag_commercial_pricing_outlier")
    planted = j.filter(pl.col("truth_outlier_type").is_in(["data_quality", "commercial"]))
    recall = planted.filter(any_flag).height / planted.height
    clean = j.filter(pl.col("truth_outlier_type").is_null() | (pl.col("truth_outlier_type") == ""))
    fpr = clean.filter(any_flag).height / clean.height
    return _row(
        "Planted outlier detection",
        "≥ 90% recall, ≤ 5% false-flag rate",
        f"Recall {recall:.1%}, false-flag rate {fpr:.2%}",
        recall >= 0.90 and fpr <= 0.05,
    )


def segmentation_checks(tmp: Path) -> list[dict[str, Any]]:
    print("segmentation ARI (seed 3, default scale, bootstrap 8)...")
    out = tmp / "seg"
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=3)
    generate(cfg, out)
    rules = Rules.model_validate({"segmentation": {"bootstrap_resamples": 8}})
    res = run_pipeline(out / "erp", MAPPING, rules)
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    df = prepare_segmentation_frame(flagged, res.reference["customers"], res.reference["items"])
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    planted = panel.select("customer_id", "item_id", "segment_id").unique()
    df = df.join(planted, on=["customer_id", "item_id"], how="left")
    result = fit_commercial_segments(df, rules, planted_segment_col="segment_id", seed=1)
    ari = result.out_of_sample_ari_vs_planted or 0.0
    boot = result.bootstrap_median_ari or 0.0
    return [
        _row(
            "Commercial segments vs planted (out-of-sample ARI)",
            "At least 0.70 on the well-identified preset",
            f"ARI {ari:.3f} (seed 3, default scale)",
            ari >= 0.70,
        ),
        _row(
            "Segment stability (bootstrap median ARI)",
            "≥ 0.80",
            f"Median ARI {boot:.3f} over 8 resamples",
            boot >= 0.80,
        ),
    ]


def elasticity_checks(tmp: Path) -> list[dict[str, Any]]:
    rules = rules_full()
    rows = []
    studies = {}
    for label, engine_seg in (("planted grain", False), ("end-to-end (engine segments)", True)):
        print(f"elasticity recovery, {label}, seeds {STUDY_SEEDS}...")
        results = []
        for seed in STUDY_SEEDS:
            panel, truth, seg_map = build_world_panel(
                tmp / f"el_{engine_seg}",
                Preset.WELL_IDENTIFIED,
                seed,
                use_engine_segments=engine_seg,
            )
            sens = run_sensitivity(
                panel,
                rules,
                market_control_source=SOURCE_MAPPED,
                market_control_grain="item_category:month",
            )
            for r in sens.to_dicts():
                pseg = seg_map.get(r["segment"])
                if pseg is None:
                    continue
                r["seed"] = seed
                r["true_elasticity"] = truth[pseg]
                results.append(r)
        studies[label] = results
        errs = sorted(abs(r["iv"] - r["true_elasticity"]) for r in results if r["iv"] is not None)
        med, p90, worst = float(np.median(errs)), float(np.quantile(errs, 0.9)), max(errs)
        rows.append(
            _row(
                f"Elasticity recovery, {label}, {len(STUDY_SEEDS)} seeds",
                "Median abs error ≤ 0.10; P90 ≤ 0.15 (worst reported)",
                f"Median {med:.3f}, P90 {p90:.3f}, worst {worst:.3f}",
                med <= 0.10 and p90 <= 0.15,
            )
        )
    study = studies["planted grain"]
    by_seed: dict[int, list[bool]] = {}
    for r in study:
        if r["naive_fe_ols"] is not None:
            by_seed.setdefault(r["seed"], []).append(r["naive_fe_ols"] > r["true_elasticity"])
    biased_all = all(all(v) for v in by_seed.values()) and bool(by_seed)
    rows.append(
        _row(
            "Naive estimator bias",
            "Biased in the planted direction (toward zero) on every seed",
            f"Toward zero on {sum(all(v) for v in by_seed.values())}/{len(by_seed)} seeds, every segment",
            biased_all,
        )
    )
    reachable = any(r["status"] == STATUS_CAUSAL for r in study)
    causal_rows = [r for r in study if r["status"] == STATUS_CAUSAL]
    paths = sorted({str(r.get("competitor_path_resolution")) for r in causal_rows})
    controls = sorted({str(r.get("market_control_status")) for r in causal_rows})
    rows.append(
        _row(
            "Causal support reachable",
            "Reached on the well-identified preset with all criteria enforced",
            (
                f"Reached; competitor-price path {', '.join(paths)}; "
                f"market index {', '.join(controls)}"
                if reachable
                else "Never reached"
            ),
            reachable,
        )
    )
    # Informational (Run 6, Phase 70): what the competitor-price gate does to
    # the status ladder, measured rather than asserted.
    ident = [r["n_identified_instruments"] for r in study if r.get("n_identified_instruments")]
    just_identified = sum(1 for n in ident if int(n) < 2)
    rows.append(
        _row(
            "Competitor-price path gate",
            "Informational: how the Phase 70 gate lands on the well-identified preset",
            (
                f"{len(causal_rows)}/{len(study)} segment-seeds reach causal support; "
                f"{just_identified}/{len(ident)} are just-identified after controlling "
                "(a category tariff instrument is itself a category market-price mover, "
                "so holding the market price constant leaves the supplier cost shock as "
                "the identifying variation)"
            ),
            True,
        )
    )
    # Informational (Run 6, Phase 71): the robust weak-instrument diagnostics,
    # measured on worlds that have no planted anticipation channel, so a
    # rejection of the lead test there is a false positive by construction.
    lead = [str(r.get("lead_test_status")) for r in study]
    rejected = sum(1 for s in lead if s == "lead_test_rejected")
    passed_lead = sum(1 for s in lead if s == "lead_test_passed")
    inconclusive = sum(1 for s in lead if s == "lead_test_inconclusive")
    immaterial = sum(1 for s in lead if s == "lead_test_immaterial")
    bounds = sorted(
        float(r["lead_test_bound"])
        for r in study
        if r.get("lead_test_bound") is not None and r.get("lead_test_status") == "lead_test_passed"
    )
    rows.append(
        _row(
            "Anticipation (lead) test false-rejection rate",
            "Informational: on the well-identified preset no anticipation channel is "
            "planted, so every rejection is a false positive",
            (
                f"{rejected}/{len(lead)} segment-seeds rejected as material, "
                f"{passed_lead} passed, {immaterial} detected but below the materiality "
                f"threshold, {inconclusive} reported inconclusive for lack of power"
                + (
                    f"; smallest detectable effect {bounds[0]:.2f}-{bounds[-1]:.2f} "
                    "log-volume units where it passes"
                    if bounds
                    else ""
                )
            ),
            True,
        )
    )
    f_eff = sorted(
        float(r["effective_first_stage_f"])
        for r in study
        if r.get("effective_first_stage_f") is not None
    )
    heur = sorted(float(r["first_stage_f"]) for r in study if r.get("first_stage_f") is not None)
    shapes: dict[str, int] = {}
    for r in study:
        shapes[str(r.get("ar_shape"))] = shapes.get(str(r.get("ar_shape")), 0) + 1
    measures = sorted(float(r["ar_measure"]) for r in study if r.get("ar_measure") is not None)
    rows.append(
        _row(
            "Robust first-stage statistic and robust-set shape",
            "Informational: the Montiel Olea-Pflueger effective F against the published "
            "23.109 bar, and the shape of the weak-instrument-robust set",
            (
                (
                    f"Effective F {f_eff[0]:.0f}-{f_eff[-1]:.0f} "
                    f"(heuristic Wald/K {heur[0]:.0f}-{heur[-1]:.0f}); "
                    if f_eff and heur
                    else "Effective F not computed; "
                )
                + "robust set "
                + ", ".join(
                    f"{k.replace('ar_set_', '').replace('_', ' ')} x{v}"
                    for k, v in sorted(shapes.items())
                )
                + (
                    f"; total accepted width {measures[0]:.2f}-{measures[-1]:.2f}"
                    if measures
                    else ""
                )
            ),
            True,
        )
    )
    return rows


def coverage_check(tmp: Path) -> dict[str, Any]:
    print("95% CI coverage, 50 simulations (seeds 100-149)... (the long one)")
    rules = rules_full()
    covered, total = 0, 0
    for seed in range(100, 150):
        panel, truth, _ = build_world_panel(
            tmp / "cov", Preset.WELL_IDENTIFIED, seed, use_engine_segments=False
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
            # Coverage studies measure the CONDITIONAL estimator's intervals;
            # the total-quantity estimand is not read here (Run 6, Phase 72).
            with_total_quantity=False,
        )
        for r in sens.to_dicts():
            if r["iv_ci_low"] is None:
                continue
            t = truth[r["segment"]]
            total += 1
            covered += int(r["iv_ci_low"] - 1e-9 <= t <= r["iv_ci_high"] + 1e-9)
    rate = covered / total
    return _row(
        "95% CI coverage, 50 simulations",
        "Covers truth 90-98% of the time",
        f"{covered}/{total} = {rate:.1%}",
        0.90 <= rate <= 0.98,
    )


CLUSTER_LEVELS = (
    "supplier",
    "supplier_month",
    "category",
    "category_month",
    "month",
    "month_customer",
)


def cluster_structure_coverage(tmp: Path) -> list[dict[str, Any]]:
    """Run 6 (Phase 73, Finding 4): coverage by clustering option, on the two
    worlds that put the choice under pressure.

    `persistent_demand` carries AR(1) customer demand, which is what picked the
    Run 4 default. `few_clusters` adds the problem this phase is about: five
    suppliers, four categories, twelve months and strongly serially correlated
    supplier and category shocks, so the units the instruments are ASSIGNED to
    are few and each one's movement is correlated across the whole panel.

    Worlds are generated once per seed and re-estimated per option, so the
    comparison is apples-to-apples. Besides coverage, each row reports how
    often the option produced NO interval at all - an option that covers well
    by refusing to answer is not the better option, and the count says so.
    """
    worlds = (Preset.PERSISTENT_DEMAND, Preset.FEW_CLUSTERS)
    # 25 seeds rather than the 50 the Run 4 three-level study used: twice the
    # worlds and twice the options already double the estimation work on the
    # same number of generated worlds, and the margins being separated here
    # (95% against 84%) are far wider than the sampling noise at 25 x 6
    # segment-estimates per cell.
    seeds = range(100, 125)
    base = rules_full()
    shipped = base.sensitivity.cluster_level
    tally: dict[tuple[str, str], list[int]] = {
        (w.value, lv): [0, 0, 0] for w in worlds for lv in CLUSTER_LEVELS
    }
    print(
        f"cluster-structure coverage, {len(seeds)} simulations x 2 worlds "
        "x 6 clusterings... (one of the long ones)"
    )
    for world in worlds:
        for seed in seeds:
            panel, truth, _ = build_world_panel(
                tmp / "ccov", world, seed, use_engine_segments=False
            )
            for lv in CLUSTER_LEVELS:
                rules = base.model_copy(
                    update={
                        "sensitivity": base.sensitivity.model_copy(update={"cluster_level": lv})
                    }
                )
                sens = run_sensitivity(
                    panel,
                    rules,
                    market_control_source=SOURCE_MAPPED,
                    market_control_grain="item_category:month",
                    # Coverage studies measure the CONDITIONAL estimator's
                    # intervals; the total-quantity estimand is not read here.
                    with_total_quantity=False,
                )
                cell = tally[(world.value, lv)]
                for r in sens.to_dicts():
                    if r.get("iv") is None:
                        continue
                    if r["iv_ci_low"] is None:
                        cell[2] += 1
                        continue
                    t = truth[r["segment"]]
                    cell[1] += 1
                    cell[0] += int(r["iv_ci_low"] - 1e-9 <= t <= r["iv_ci_high"] + 1e-9)
            del panel
    rows = []
    for world in worlds:
        # Only the persistent-demand world GATES. `few_clusters` is built to
        # have too few groups to work with, so the right outcome there is the
        # engine declining to report intervals - which would score as 0%
        # coverage and fail a coverage target that was never the point. Those
        # rows say what happened and are read, not gated.
        gates = world is Preset.PERSISTENT_DEMAND
        for lv in CLUSTER_LEVELS:
            covered, total, withdrawn = tally[(world.value, lv)]
            rate = covered / total if total else 0.0
            is_default = lv == shipped
            name = world.value.replace("_", " ")
            achieved = f"{covered}/{total} = {rate:.1%}" if total else "no intervals reported"
            if withdrawn:
                achieved += f", {withdrawn} withdrawn for having too few groups"
            rows.append(
                _row(
                    f"95% CI coverage on the {name} world, grouped by "
                    f"{CLUSTER_LEVEL_NAMES[lv]}"
                    + (" (shipped default)" if is_default else " (alternative)"),
                    "Covers truth 90-98% of the time"
                    if (gates and is_default)
                    else "Informational - reported to justify the clustering default",
                    achieved,
                    (0.90 <= rate <= 0.98) if (gates and is_default) else True,
                )
            )
    return rows


def misspecified_checks(tmp: Path) -> list[dict[str, Any]]:
    """Run 4 (24.9): recovery and status outcomes on misspecified worlds.
    Informational, never gated - the point is to show what breaks and that
    statuses respond honestly."""
    rules = rules_full()
    rows = []
    for preset in (
        Preset.PERSISTENT_DEMAND,
        Preset.HETEROGENEOUS_ELASTICITY,
        Preset.NONCONSTANT_ELASTICITY,
        Preset.COMPETITOR_COMOVEMENT,
    ):
        print(f"misspecified world {preset.value} (3 seeds)...")
        errs: list[float] = []
        statuses: dict[str, int] = {}
        for seed in (5, 6, 7):
            panel, truth, _ = build_world_panel(
                tmp / "miss", preset, seed, use_engine_segments=False
            )
            sens = run_sensitivity(
                panel,
                rules,
                market_control_source=SOURCE_MAPPED,
                market_control_grain="item_category:month",
            )
            for r in sens.to_dicts():
                statuses[r["status"]] = statuses.get(r["status"], 0) + 1
                if r["iv"] is not None and r["segment"] in truth:
                    errs.append(abs(r["iv"] - truth[r["segment"]]))
        med = float(np.median(errs)) if errs else float("nan")
        status_str = ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items()))
        rows.append(
            _row(
                f"Misspecified world: {preset.value} (informational)",
                "Informational - reported, not gated (the planted beta is not the "
                "estimand under misspecification)",
                f"median abs error vs planted {med:.3f}; statuses {{{status_str}}}",
                True,
            )
        )
    return rows


def preset_checks(tmp: Path) -> list[dict[str, Any]]:
    rules = rules_full()
    rows = []
    print("weak_instrument preset (seed 3)...")
    panel, _, _ = build_world_panel(
        tmp / "weak", Preset.WEAK_INSTRUMENT, 3, use_engine_segments=False
    )
    sens = run_sensitivity(
        panel,
        rules,
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
    )
    weak_ok = all(r["status"] != STATUS_CAUSAL for r in sens.to_dicts())
    rows.append(
        _row(
            "weak-instrument preset",
            "Never reaches causal support; weak-IV-robust interval reported",
            "No segment reached causal support; Anderson-Rubin set reported"
            if weak_ok
            else "FAILED",
            weak_ok,
        )
    )
    print("invalid_instrument preset (5 seeds, 250x400)...")
    rejected, downgraded, n = 0, 0, 0
    for seed in (3, 11, 19, 27, 35):
        panel, _, _ = build_world_panel(
            tmp / "inv",
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
        n += 1
        if any(r["overid_p"] < 0.05 for r in seg_rows):
            rejected += 1
        if all(r["status"] != STATUS_CAUSAL for r in seg_rows if r["overid_p"] < 0.05):
            downgraded += 1
    rows.append(
        _row(
            "invalid-instrument preset",
            "Overidentification rejects on ≥ 80% of seeds; status downgraded",
            f"Rejected on {rejected}/{n} seeds; downgraded on {downgraded}/{n}",
            rejected >= 0.8 * n and downgraded == n,
        )
    )
    for preset, name in ((Preset.PRICE_CONSTANT, "price_constant"), (Preset.SPARSE, "sparse")):
        print(f"{name} preset (seed 3)...")
        panel, _, _ = build_world_panel(tmp / name, preset, 3, use_engine_segments=False)
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
        )
        ok = all(r["status"] in (STATUS_INSUFFICIENT, STATUS_DESCRIPTIVE) for r in sens.to_dicts())
        rows.append(
            _row(
                f"{name.replace(chr(95), chr(45))} preset",
                "Insufficient data or descriptive only - never higher",
                "Held for every segment" if ok else "FAILED",
                ok,
            )
        )
    return rows


def total_quantity_checks(tmp: Path) -> list[dict[str, Any]]:
    """Recovery of the TOTAL-quantity elasticity on worlds that plant a real
    price-driven extensive margin (Run 6, Phase 72).

    Two rows. The first is the one that matters: the estimate must be closer to
    the planted TOTAL elasticity than to the planted conditional one, or it is
    not measuring the extensive margin at all. The second records the accuracy,
    which is the measured reason the ladder has no causal tier.
    """
    import json as _json

    from margin_engine.panel.build import build_panel
    from margin_engine.sensitivity.estimate import STATUS_CAUSAL as _CAUSAL

    rules = rules_full()
    # The shipped pipeline runs with no resampled interval, because a replicate
    # refits the whole procedure and the measured pipeline budget does not
    # carry it. This study is where that inference is measured, so it is turned
    # on here and the cost is paid once.
    rules = rules.model_copy(
        update={
            "sensitivity": rules.sensitivity.model_copy(
                update={
                    "iv_ppml_bootstrap_reps": 60,
                    "iv_ppml_split_panel_jackknife": True,
                }
            )
        }
    )
    print("total-quantity recovery, extensive_margin preset, seeds 42/7/13...")
    to_total: list[float] = []
    to_cond: list[float] = []
    to_spj: list[float] = []
    se_ratio: list[float] = []
    range_covered = range_total = 0
    statuses: set[str] = set()
    for seed in (42, 7, 13):
        out = tmp / f"extm_{seed}"
        cfg = GeneratorConfig.for_preset(
            Preset.EXTENSIVE_MARGIN, seed=seed, n_customers=200, n_items=300
        )
        generate(cfg, out)
        truth = {
            s["segment_id"]: s
            for s in _json.loads((out / "ground_truth" / "truth_summary.json").read_text())[
                "segments"
            ]
        }
        res = run_pipeline(out / "erp", MAPPING, rules)
        flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
        pt = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
        obs = flagged.join(
            pt.select("customer_id", "item_id", "segment_id").unique(),
            on=["customer_id", "item_id"],
            how="left",
        ).with_columns(pl.col("segment_id").alias("segment_leaf"))
        panel = build_panel(
            obs,
            res.reference["items"],
            res.reference["supplier_cost_index"],
            res.reference["tariff_schedule"],
            rules,
            quotes=res.reference.get("quotes"),
            price_list=res.reference.get("price_list"),
        )
        sens = run_sensitivity(
            panel,
            rules,
            market_control_source=SOURCE_MAPPED,
            market_control_grain="item_category:month",
        )
        for r in sens.to_dicts():
            s = truth.get(r["segment"])
            statuses.add(str(r.get("iv_ppml_status")))
            if s is None or "total_elasticity" not in s or r.get("iv_ppml") is None:
                continue
            if (r.get("imputed_price_share") or 0.0) > 0.5:
                continue
            to_total.append(abs(r["iv_ppml"] - s["total_elasticity"]))
            to_cond.append(abs(r["iv_ppml"] - s["elasticity"]))
            if r.get("iv_ppml_spj") is not None:
                to_spj.append(abs(r["iv_ppml_spj"] - s["total_elasticity"]))
            if r.get("iv_ppml_se_bootstrap") and r.get("iv_ppml_se_analytic"):
                se_ratio.append(r["iv_ppml_se_bootstrap"] / r["iv_ppml_se_analytic"])
            if r.get("iv_ppml_range_low") is not None:
                range_total += 1
                range_covered += int(
                    r["iv_ppml_range_low"] <= s["total_elasticity"] <= r["iv_ppml_range_high"]
                )
    rows: list[dict[str, Any]] = []
    closer = bool(to_total) and float(np.median(to_total)) < float(np.median(to_cond))
    rows.append(
        _row(
            "Total-quantity estimate measures the extensive margin",
            "Closer to the planted TOTAL elasticity than to the planted conditional one",
            (
                f"Median absolute error {np.median(to_total):.3f} against the total "
                f"elasticity and {np.median(to_cond):.3f} against the conditional one "
                f"({len(to_total)} segment-seeds)"
                if to_total
                else "No total-quantity estimate was produced"
            ),
            closer,
        )
    )
    rows.append(
        _row(
            "Total-quantity recovery accuracy (informational)",
            "Informational: the measured accuracy that caps this ladder below the "
            "conditional one - no causal tier is awarded on it",
            (
                f"Median {np.median(to_total):.3f}, P90 {np.quantile(to_total, 0.9):.3f}, "
                f"worst {max(to_total):.3f} against the conditional estimator's gated "
                f"0.10 median and 0.15 P90; statuses reached: "
                f"{', '.join(sorted(s.replace('_', ' ') for s in statuses if s != 'None'))}"
                + ("; no causal status reached" if _CAUSAL not in statuses else "")
                if to_total
                else "Not measured"
            ),
            True,
        )
    )
    rows.append(
        _row(
            "Total-quantity resampled interval (informational)",
            "Informational: what the pair cluster bootstrap adds over the analytic "
            "cluster-robust standard error, and whether its range covers the planted "
            "total elasticity",
            (
                f"Bootstrap standard error is {np.median(se_ratio):.2f}x the analytic one "
                f"at the median over {len(se_ratio)} segment-seeds, the difference being "
                f"the first-stage sampling noise the analytic figure omits; the 95% "
                f"resampled range covers the planted total elasticity in "
                f"{range_covered}/{range_total} cases"
                if se_ratio and range_total
                else "Not measured"
            ),
            True,
        )
    )
    rows.append(
        _row(
            "Split-panel jackknife evaluated, not adopted (informational)",
            "Informational: the half-panel bias correction for IV-PPML with "
            "high-dimensional fixed effects is computed and reported; it is adopted as "
            "the point estimate only if it measurably improves recovery",
            (
                f"Uncorrected median absolute error {np.median(to_total):.3f} against "
                f"corrected {np.median(to_spj):.3f} over {len(to_spj)} segment-seeds "
                f"(P90 {np.quantile(to_total, 0.9):.3f} against "
                f"{np.quantile(to_spj, 0.9):.3f}). "
                + (
                    "The correction does not improve recovery here, so it is reported as a "
                    "diagnostic and the uncorrected estimate stays the point estimate."
                    if not to_spj or np.median(to_spj) >= np.median(to_total)
                    else "The correction improves recovery on these worlds; see "
                    "docs/methodology.md for the adoption decision."
                )
                if to_spj
                else "Not measured"
            ),
            True,
        )
    )
    return rows


def kvi_check(tmp: Path) -> dict[str, Any]:
    print("KVI candidate recall (seed 12, 100x250)...")
    out = tmp / "kvi"
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=12, n_customers=100, n_items=250)
    generate(cfg, out)
    rules = load_rules(REPO / "config" / "rules.yaml")
    res = run_pipeline(out / "erp", MAPPING, rules)
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    scores = kvi_candidate_scores(flagged, rules)
    planted = set(res.reference["items"].filter(pl.col("item_role") == "kvi")["item_id"].to_list())
    candidates = set(scores.filter(pl.col("is_kvi_candidate"))["item_id"].to_list())
    recall = len(planted & candidates) / len(planted)
    return _row(
        "KVI candidates",
        "≥ 80% of planted KVIs in top-ranked candidates",
        f"Recall {recall:.1%} ({len(planted)} planted)",
        recall >= 0.80,
    )


def win_curve_check(tmp: Path) -> dict[str, Any]:
    print("win-curve recovery (seed 2, 200x400)...")
    out = tmp / "quotes"
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=2, n_customers=200, n_items=400)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, load_rules(REPO / "config" / "rules.yaml"))
    pt = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    seg_map = pt.select(
        "customer_id", "item_id", pl.col("segment_id").alias("segment_leaf")
    ).unique()
    qi = quote_price_index(res.reference["quotes"], res.reference["price_list"])
    curves = fit_win_curves(qi, seg_map)
    truth = json.loads((out / "ground_truth" / "truth_summary.json").read_text())["quotes"][
        "win_curve"
    ]
    errs = [
        abs(r["price_coef"] - truth[str(r["segment"])]["b"]) / abs(truth[str(r["segment"])]["b"])
        for r in curves.to_dicts()
        if r["status"] == STATUS_ESTIMATED
    ]
    worst = max(errs) if errs else 1.0
    return _row(
        "Win-curve price coefficient",
        "Within ±15% relative of planted, per estimated segment",
        f"Worst estimated segment {worst:.1%} ({len(errs)} estimated, precision-gated)",
        bool(errs) and worst <= 0.15,
    )


def determinism_check(tmp: Path) -> dict[str, Any]:
    print("bundle determinism (committed sample, two runs)...")
    from margin_engine.report.full_run import run_full
    from margin_engine.webexport.build import build_bundle, write_bundle

    rules_path = REPO / "config" / "rules.yaml"
    hashes = []
    for i in (1, 2):
        res = run_full(REPO / "data" / "sample" / "erp", MAPPING, rules_path, tmp / f"det{i}")
        bundle = build_bundle(res, load_rules(rules_path), REPO)
        p, _ = write_bundle(bundle, tmp / f"detb{i}")
        hashes.append(hashlib.sha256(p.read_bytes()).hexdigest())
    ok = hashes[0] == hashes[1]
    return _row(
        "Engine determinism",
        "Byte-identical bundle across runs on the same input",
        "Byte-identical" if ok else "DIFFERS",
        ok,
    )


def _generate_world_in_child(out: Path, *, n_customers: int, n_items: int, seed: int) -> None:
    """Generate a world in a subprocess, so its peak memory goes back to the
    operating system before the caller measures anything."""
    script = textwrap.dedent(f"""
        from pathlib import Path
        from synth.config import GeneratorConfig, Preset
        from synth.generate import generate

        cfg = GeneratorConfig.for_preset(
            Preset.WELL_IDENTIFIED, seed={seed}, n_customers={n_customers}, n_items={n_items}
        )
        generate(cfg, Path(r"{out}"))
    """)
    subprocess.run(
        [sys.executable, "-c", script], cwd=str(REPO), check=True, capture_output=True, text=True
    )


def performance_check(tmp: Path) -> list[dict[str, Any]]:
    """The runtime target, measured where it can be measured honestly.

    Two rows, because one number cannot do both jobs.

    The section 8 figure is produced by the benchmark CI job, not here. The
    full world needs about 5.4 GB, which does not fit beside a desktop on the
    14 GB development machine; and the absolute figure moves several seconds
    between machines and between runs on one machine, so a single local
    reading cannot separate a code regression from a slower laptop - which is
    exactly the confusion it caused in Phase 71, where unchanged code measured
    118.3 to 120.3s against a committed 111.0s. CI measures the branch and
    `main` back to back on one runner and gates both the absolute bar and a
    10% regression against main.

    What runs here is a reduced-scale smoke, which keeps the whole pipeline
    exercised on a developer machine inside the memory floor. It is reported
    as a smoke and never as the target: it is below the 500k lines the target
    names, so it cannot pass it.
    """
    import platform

    machine = f"{platform.machine()} ({platform.system()})"
    rows = [
        _row(
            "Performance (full Run-1 pipeline)",
            "500k+ lines under 2 minutes",
            "Measured by the benchmark CI job (.github/workflows/benchmark.yml) on a fixed "
            "runner, which measures this branch and main back to back and fails the branch "
            "if it is over the bar or more than 10% slower than main. Not measured on a "
            "developer machine: the world alone needs about 5.4 GB, and a single local "
            "reading cannot tell a code regression from a slower machine.",
            True,
        )
    ]

    print("performance smoke (reduced scale, developer machine)...")
    from margin_engine.report.full_run import run_full

    out = tmp / "perf"
    # Generated in a CHILD process so the generator's peak and the pipeline's
    # peak never coexist; the timer covers run_full alone, on a world already
    # written to disk.
    _generate_world_in_child(out, n_customers=250, n_items=400, seed=1)
    t0 = time.perf_counter()
    res = run_full(out / "erp", MAPPING, REPO / "config" / "rules.yaml", tmp / "perf_out")
    elapsed = time.perf_counter() - t0
    lines = res.pipeline.ledger.height
    rows.append(
        _row(
            "Performance smoke (reduced scale, developer machine)",
            "Completes end to end inside the development machine's memory floor; "
            "does NOT measure the section 8 target, which is smaller than 500k lines here",
            f"{lines:,} lines in {elapsed:.1f}s on {machine}",
            lines > 0,
        )
    )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the coverage study and perf run")
    ap.add_argument(
        "--only-perf",
        action="store_true",
        help="re-measure only the performance check and update it in docs/validation.json",
    )
    args = ap.parse_args()
    t0 = time.perf_counter()
    out_path = REPO / "docs" / "validation.json"
    if args.only_perf:
        with tempfile.TemporaryDirectory() as td:
            perf_rows = performance_check(Path(td))
        by_check = {r["check"]: r for r in perf_rows}
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        existing = {r["check"] for r in payload["results"]}
        payload["results"] = [by_check.get(r["check"], r) for r in payload["results"]]
        # A row that did not exist before (the smoke) is appended rather than
        # dropped on the floor.
        payload["results"] += [r for c, r in by_check.items() if c not in existing]
        payload["runtime_s"] = round(payload.get("runtime_s", 0) + time.perf_counter() - t0, 1)
        out_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"updated {out_path}")
        sys.exit(0 if all(r["passed"] for r in perf_rows) else 1)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        results += fixture_checks()
        results.append(outlier_check(tmp))
        results += segmentation_checks(tmp)
        results += elasticity_checks(tmp)
        if not args.quick:
            results.append(coverage_check(tmp))
            results += cluster_structure_coverage(tmp)
        results += preset_checks(tmp)
        results += misspecified_checks(tmp)
        results += total_quantity_checks(tmp)
        results.append(kvi_check(tmp))
        results.append(win_curve_check(tmp))
        results.append(determinism_check(tmp))
        if not args.quick:
            results += performance_check(tmp)
    payload = {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "script": "scripts/measure_validation.py" + (" --quick" if args.quick else ""),
        "runtime_s": round(time.perf_counter() - t0, 1),
        "note": "Every achieved value is measured by this script (or by running the named test files); nothing is hand-typed.",
        "results": results,
    }
    out_path = REPO / "docs" / "validation.json"
    # Other measurement scripts (measure_hybrid_world, measure_tracker,
    # measure_scale, measure_scenario_matrix) contribute their own rows to
    # this file. Re-running THIS script must not delete their evidence, so
    # any pre-existing row this run did not produce is carried forward.
    carried: list[dict[str, Any]] = []
    if out_path.exists():
        try:
            previous = json.loads(out_path.read_text(encoding="utf-8")).get("results", [])
        except (OSError, json.JSONDecodeError):
            previous = []
        mine = {str(r["check"]) for r in results}
        carried = [r for r in previous if str(r.get("check")) not in mine]
        if carried:
            print(f"  carrying forward {len(carried)} row(s) from other measurement scripts")
    payload["results"] = results + carried
    out_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8", newline="\n")
    n_pass = sum(r["passed"] for r in results)
    print(f"\nwrote {out_path} - {n_pass}/{len(results)} checks passed, {payload['runtime_s']}s")
    if n_pass != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
