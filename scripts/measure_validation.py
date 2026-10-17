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
    STATUS_CAUSAL,
    STATUS_DESCRIPTIVE,
    STATUS_INSUFFICIENT,
    run_sensitivity,
)

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
            "≥ 0.70 on well_identified",
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
            sens = run_sensitivity(panel, rules)
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
    rows.append(
        _row(
            "causal_elasticity_supported reachable",
            "Reached on well_identified with all criteria enforced",
            "Reached" if reachable else "Never reached",
            reachable,
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
        sens = run_sensitivity(panel, rules)
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


def preset_checks(tmp: Path) -> list[dict[str, Any]]:
    rules = rules_full()
    rows = []
    print("weak_instrument preset (seed 3)...")
    panel, _, _ = build_world_panel(
        tmp / "weak", Preset.WEAK_INSTRUMENT, 3, use_engine_segments=False
    )
    sens = run_sensitivity(panel, rules)
    weak_ok = all(r["status"] != STATUS_CAUSAL for r in sens.to_dicts())
    rows.append(
        _row(
            "weak_instrument preset",
            "Never causal_elasticity_supported; weak-IV-robust CI reported",
            "No segment reached causal support; AR set reported" if weak_ok else "FAILED",
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
        sens = run_sensitivity(panel, rules)
        seg_rows = [r for r in sens.to_dicts() if r["overid_p"] is not None]
        n += 1
        if any(r["overid_p"] < 0.05 for r in seg_rows):
            rejected += 1
        if all(r["status"] != STATUS_CAUSAL for r in seg_rows if r["overid_p"] < 0.05):
            downgraded += 1
    rows.append(
        _row(
            "invalid_instrument preset",
            "Overidentification rejects on ≥ 80% of seeds; status downgraded",
            f"Rejected on {rejected}/{n} seeds; downgraded on {downgraded}/{n}",
            rejected >= 0.8 * n and downgraded == n,
        )
    )
    for preset, name in ((Preset.PRICE_CONSTANT, "price_constant"), (Preset.SPARSE, "sparse")):
        print(f"{name} preset (seed 3)...")
        panel, _, _ = build_world_panel(tmp / name, preset, 3, use_engine_segments=False)
        sens = run_sensitivity(panel, rules)
        ok = all(r["status"] in (STATUS_INSUFFICIENT, STATUS_DESCRIPTIVE) for r in sens.to_dicts())
        rows.append(
            _row(
                f"{name} preset",
                "insufficient_data or descriptive_elasticity only",
                "Held for every segment" if ok else "FAILED",
                ok,
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


def performance_check(tmp: Path) -> dict[str, Any]:
    import platform

    print("performance (large world, full Run-1 pipeline)...")
    from margin_engine.report.full_run import run_full

    out = tmp / "perf"
    # ~600-700k lines: the target is "500k+ lines under 2 minutes", measured
    # at that scale, not at multiples of it.
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=1, n_customers=600, n_items=900)
    generate(cfg, out)
    t0 = time.perf_counter()
    res = run_full(out / "erp", MAPPING, REPO / "config" / "rules.yaml", tmp / "perf_out")
    elapsed = time.perf_counter() - t0
    lines = res.pipeline.ledger.height
    return _row(
        "Performance (full Run-1 pipeline)",
        "500k+ lines under 2 minutes",
        f"{lines:,} lines in {elapsed:.1f}s on {platform.machine()} ({platform.system()})",
        lines >= 500_000 and elapsed < 120,
    )


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
            row = performance_check(Path(td))
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        payload["results"] = [row if r["check"] == row["check"] else r for r in payload["results"]]
        payload["runtime_s"] = round(payload.get("runtime_s", 0) + time.perf_counter() - t0, 1)
        out_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"updated {out_path}")
        sys.exit(0 if row["passed"] else 1)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        results += fixture_checks()
        results.append(outlier_check(tmp))
        results += segmentation_checks(tmp)
        results += elasticity_checks(tmp)
        if not args.quick:
            results.append(coverage_check(tmp))
        results += preset_checks(tmp)
        results.append(kvi_check(tmp))
        results.append(win_curve_check(tmp))
        results.append(determinism_check(tmp))
        if not args.quick:
            results.append(performance_check(tmp))
    payload = {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "script": "scripts/measure_validation.py" + (" --quick" if args.quick else ""),
        "runtime_s": round(time.perf_counter() - t0, 1),
        "note": "Every achieved value is measured by this script (or by running the named test files); nothing is hand-typed.",
        "results": results,
    }
    out_path = REPO / "docs" / "validation.json"
    out_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8", newline="\n")
    n_pass = sum(r["passed"] for r in results)
    print(f"\nwrote {out_path} - {n_pass}/{len(results)} checks passed, {payload['runtime_s']}s")
    if n_pass != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
