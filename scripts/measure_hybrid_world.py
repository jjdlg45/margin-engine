"""Run 4 (29.4): hybrid demonstration world — supplier cost paths driven by
REAL BLS PPI series (via the connector cache or the committed fixture)
instead of simulated shocks, so the instrument pipeline runs on real-world
cost movements. Demand stays simulated, so the planted elasticity remains the
known truth. Recovery is reported in validation.json as INFORMATIONAL (the
spec gates nothing on it).

Usage:
    uv run python scripts/measure_hybrid_world.py --external-cost-index          # fixture/cache
    uv run python scripts/measure_hybrid_world.py --external-cost-index --live   # refresh from BLS
    ... --dry   (measure only, no validation.json update)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from synth.config import Preset  # noqa: E402

CHECK_NAME = "Hybrid world: real PPI cost paths (informational)"
SERIES = ["WPU101", "WPU081", "WPU0638"]
FIXTURE = REPO / "tests" / "fixtures" / "bls_ppi_2016_2025.json"
SEED = 11


def ppi_paths(months: int, live: bool) -> tuple[list[list[float]], list[str]]:
    """Demeaned log PPI levels per series, most recent `months` observations."""
    from margin_engine.connectors.bls import fetch_ppi_series

    cache_dir = REPO / ".connector_cache"
    df, notes = fetch_ppi_series(
        SERIES,
        2016,
        2025,
        cache_dir,
        fixture=None if live else FIXTURE,
        max_age_days=30.0 if live else None,
    )
    paths: list[list[float]] = []
    for sid in SERIES:
        vals = df.filter(pl.col("series_id") == sid).sort("period")["value"].to_numpy()
        logs = np.log(vals[-months:])
        paths.append(list(logs - logs.mean()))
    return paths, notes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--external-cost-index",
        action="store_true",
        help="drive supplier cost paths from the real PPI series (29.4)",
    )
    ap.add_argument("--live", action="store_true", help="refresh from BLS instead of the fixture")
    ap.add_argument("--dry", action="store_true", help="do not update validation.json")
    args = ap.parse_args()
    if not args.external_cost_index:
        print("nothing to do: pass --external-cost-index")
        sys.exit(2)

    from tests.test_sensitivity import build_world_panel, rules_full

    from margin_engine.sensitivity.estimate import run_sensitivity

    months = 24
    paths, notes = ppi_paths(months, args.live)
    for n in notes:
        print("NOTE:", n)
    sds = [float(np.std(p)) for p in paths]
    print(f"PPI paths: {len(paths)} series, within-series log sd {[round(s, 4) for s in sds]}")

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        panel, truth, seg_map = build_world_panel(
            Path(td),
            Preset.WELL_IDENTIFIED,
            SEED,
            use_engine_segments=False,
            size={"external_cost_paths": paths},
        )
        sens = run_sensitivity(panel, rules_full())
    errors = []
    statuses: dict[str, int] = {}
    for r in sens.to_dicts():
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        planted = truth.get(seg_map.get(int(r["segment"]), -1))
        est = r["iv"] if r["iv"] is not None else r["naive_fe_ols"]
        if planted is not None and est is not None:
            errors.append(abs(est - planted))
    med = float(np.median(errors)) if errors else float("nan")
    achieved = (
        f"seed {SEED}, {len(SERIES)} real PPI series as supplier cost paths "
        f"(series log sd {[round(s, 3) for s in sds]}); median abs elasticity "
        f"error {med:.3f} over {len(errors)} segments; statuses {statuses}; "
        f"{time.perf_counter() - t0:.0f}s"
    )
    print(achieved)

    if not args.dry:
        out_path = REPO / "docs" / "validation.json"
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        row = {
            "check": CHECK_NAME,
            "target": "informational: recovery when real cost movements drive the instrument",
            "achieved": achieved,
            "passed": True,
            "informational": True,
        }
        rows = [r for r in payload["results"] if r["check"] != CHECK_NAME]
        rows.append(row)
        payload["results"] = rows
        payload["generated_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        out_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"updated {out_path}")


if __name__ == "__main__":
    main()
