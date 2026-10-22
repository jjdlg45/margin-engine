"""Run 4 (27.2): full pipeline at scale, measuring runtime and peak memory
(RSS of this process tree, sampled at 200 ms). Updates the scale row in
docs/validation.json.

Targets: 10M+ lines under 15 minutes and under 8 GB peak. This machine may
not have 8 GB of physical headroom, so the script is memory-aware: it checks
available memory first, then climbs a ladder of world sizes, projecting each
next rung's peak from the measured peaks so far, and stops BEFORE a rung that
would not fit (never crashing the machine). The largest size that fits is
recorded honestly with the hardware limits; if that is short of 10M lines the
row reads as a KNOWN GAP, not a pass.

Usage:
    uv run python scripts/measure_scale.py            # measure + update row
    uv run python scripts/measure_scale.py --dry      # measure only, no write
    uv run python scripts/measure_scale.py --max-gb 6 # tighter memory budget
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import platform
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from synth.config import GeneratorConfig, Preset  # noqa: E402

MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"
RULES = REPO / "config" / "rules.yaml"
CHECK_NAME = "Scale (10M-line pipeline)"
TARGET = "10M+ lines under 15 minutes and under 8 GB peak memory"
TIME_BUDGET_S = 900.0
PEAK_BUDGET_GB = 8.0
SAFETY_GB = 1.0  # headroom kept free so the machine itself stays usable
PROJECTION_FACTOR = 1.25  # projected next peak = peak/line so far x lines x this

# Ladder rungs (n_customers, n_items). Line counts are measured, not assumed;
# the top rung is calibrated to exceed 10M lines.
LADDER = [
    (200, 350),
    (300, 500),
    (450, 700),
    (600, 900),
    (1200, 1400),
    (1800, 2000),
    (2400, 2400),
    (3000, 2800),
]


class PeakRss:
    """Samples the process tree's RSS on a daemon thread."""

    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._proc = psutil.Process()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                rss = self._proc.memory_info().rss
                for child in self._proc.children(recursive=True):
                    with contextlib.suppress(psutil.Error):
                        rss += child.memory_info().rss
                self.peak = max(self.peak, rss)
            except (psutil.Error, OSError, MemoryError):
                # Sampling must never kill the measurement - under memory
                # pressure the system-wide process scan itself can fail.
                pass
            self._stop.wait(0.2)

    def __enter__(self) -> PeakRss:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def run_rung(n_customers: int, n_items: int) -> dict[str, object]:
    from synth.generate import generate

    from margin_engine.report.full_run import run_full

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = GeneratorConfig.for_preset(
            Preset.WELL_IDENTIFIED, seed=1, n_customers=n_customers, n_items=n_items
        )
        t_gen = time.perf_counter()
        generate(cfg, tmp / "world")
        gen_s = time.perf_counter() - t_gen
        gc.collect()
        with PeakRss() as peak:
            t0 = time.perf_counter()
            res = run_full(tmp / "world" / "erp", MAPPING, RULES, tmp / "out")
            elapsed = time.perf_counter() - t0
        lines = res.pipeline.ledger.height
        stages = {k: round(v, 1) for k, v in sorted(res.timings.items(), key=lambda kv: -kv[1])}
        del res
    gc.collect()
    return {
        "n_customers": n_customers,
        "n_items": n_items,
        "lines": lines,
        "gen_s": round(gen_s, 1),
        "run_s": round(elapsed, 1),
        "peak_gb": round(peak.peak / 1024**3, 2),
        "stages": stages,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry", action="store_true", help="measure only; do not update validation.json"
    )
    ap.add_argument(
        "--max-gb",
        type=float,
        default=None,
        help="memory budget in GB (default: min(8, available - safety))",
    )
    args = ap.parse_args()

    vm = psutil.virtual_memory()
    avail_gb = vm.available / 1024**3
    budget = args.max_gb if args.max_gb is not None else min(PEAK_BUDGET_GB, avail_gb - SAFETY_GB)
    hw = (
        f"{platform.machine()} ({platform.system()}), "
        f"{vm.total / 1024**3:.1f} GB RAM, {avail_gb:.1f} GB available at start"
    )
    print(f"{hw}; memory budget for this run: {budget:.1f} GB", flush=True)
    if budget < 1.0:
        print("less than 1 GB of headroom: refusing to run (close applications first)")
        sys.exit(1)

    results: list[dict[str, object]] = []
    stopped_reason = ""
    for n_c, n_i in LADDER:
        if results:
            last = results[-1]
            # Lines scale mainly with customers (assortment-driven), with a
            # weaker items effect through KVI carriage.
            lines_scale = (n_c / int(last["n_customers"])) * (  # type: ignore[call-overload]
                n_i / int(last["n_items"])  # type: ignore[call-overload]
            ) ** 0.5
            proj_lines = int(last["lines"]) * lines_scale  # type: ignore[call-overload]
            if len(results) >= 2:
                # Linear fit with intercept (fixed interpreter/libs baseline);
                # a pure ratio projection badly overestimates from one point.
                l1, p1 = int(results[0]["lines"]), float(results[0]["peak_gb"])  # type: ignore[call-overload, arg-type]
                l2, p2 = int(last["lines"]), float(last["peak_gb"])  # type: ignore[call-overload, arg-type]
                slope = (p2 - p1) / max(l2 - l1, 1)
                projected_gb = (p2 + slope * (proj_lines - l2)) * PROJECTION_FACTOR
            else:
                projected_gb = float(last["peak_gb"]) * lines_scale * PROJECTION_FACTOR  # type: ignore[arg-type]
            if projected_gb > budget:
                stopped_reason = (
                    f"next rung {n_c}x{n_i} projected ~{projected_gb:.1f} GB peak, "
                    f"over the {budget:.1f} GB budget on this machine"
                )
                print(f"STOP: {stopped_reason}", flush=True)
                break
        print(f"running rung {n_c}x{n_i}...", flush=True)
        r = run_rung(n_c, n_i)
        results.append(r)
        print(
            f"  {r['lines']:,} lines in {r['run_s']}s, peak {r['peak_gb']} GB (gen {r['gen_s']}s)",
            flush=True,
        )
        if float(r["peak_gb"]) > budget or float(r["run_s"]) > TIME_BUDGET_S:  # type: ignore[arg-type]
            stopped_reason = (
                f"rung {n_c}x{n_i} hit the limit (peak {r['peak_gb']} GB / "
                f"{r['run_s']}s against budget {budget:.1f} GB / {TIME_BUDGET_S:.0f}s)"
            )
            print(f"STOP: {stopped_reason}", flush=True)
            break

    if not results:
        print("no rung completed")
        sys.exit(1)
    # "Largest run that fits" means within the budget: a final rung that blew
    # through it is reported in the rungs list but is not the headline.
    fitting = [
        r
        for r in results
        if float(r["peak_gb"]) <= budget and float(r["run_s"]) <= TIME_BUDGET_S  # type: ignore[arg-type]
    ]
    best = fitting[-1] if fitting else results[-1]
    passed = (
        int(best["lines"]) >= 10_000_000  # type: ignore[call-overload]
        and float(best["run_s"]) < TIME_BUDGET_S  # type: ignore[arg-type]
        and float(best["peak_gb"]) < PEAK_BUDGET_GB  # type: ignore[arg-type]
    )
    stage_str = ", ".join(f"{k} {v}s" for k, v in list(best["stages"].items())[:4])  # type: ignore[union-attr]
    achieved = (
        f"largest run that fits this machine: {best['lines']:,} lines in "
        f"{best['run_s']}s, peak {best['peak_gb']} GB on {hw}; slowest stages: {stage_str}"
    )
    if not passed:
        achieved += (
            f". KNOWN GAP: the 10M-line target could not be verified on this "
            f"hardware ({stopped_reason or 'top rung below 10M lines'}); "
            f"the pipeline's memory use is linear in lines across the measured "
            f"rungs, but that is an extrapolation, not a measurement."
        )
    print(f"\n{achieved}\npassed: {passed}", flush=True)
    print(json.dumps(results, indent=1))

    if not args.dry:
        out_path = REPO / "docs" / "validation.json"
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        row = {
            "check": CHECK_NAME,
            "target": TARGET,
            "achieved": achieved,
            "passed": passed,
            "rungs": results,
        }
        rows = [r for r in payload["results"] if r["check"] != CHECK_NAME]
        rows.append(row)
        payload["results"] = rows
        payload["generated_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        out_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"updated {out_path}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
