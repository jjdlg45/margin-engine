"""Time the Run-1 full pipeline once and print the result as JSON.

Used by the benchmark CI job, which runs this on the pull request's code and
on `main` on the SAME runner and compares the two. A single machine measuring
both sides is the point: the absolute figure moves several seconds between
machines and even between runs on one machine, so an absolute bar alone cannot
tell a code regression from a slower runner. The comparison can.

The world is generated in a CHILD process so the generator's peak and the
pipeline's peak never coexist, and the timer covers `run_full` alone, on a
world already written to disk.

    python scripts/benchmark_pipeline.py --out bench.json [--scale full|smoke]

`--scale smoke` is the reduced size that fits under a developer machine's
memory floor. It does NOT measure the section 8 target, and says so in its
output, so a smoke number can never be mistaken for the real one.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"

# The full scale is the one section 8's "500k+ lines under 2 minutes" is
# measured at; the smoke scale exists only to keep the code path exercised
# where the full world will not fit in memory.
SCALES: dict[str, dict[str, int]] = {
    "full": {"n_customers": 600, "n_items": 900},
    "smoke": {"n_customers": 250, "n_items": 400},
}
TARGET_SECONDS = 120.0
TARGET_MIN_LINES = 500_000


def _generate_world_in_child(out: Path, *, n_customers: int, n_items: int, seed: int) -> None:
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True, help="where to write the JSON result")
    ap.add_argument("--scale", choices=sorted(SCALES), default="full")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    from margin_engine.report.full_run import run_full

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = tmp / "world"
        _generate_world_in_child(world, seed=args.seed, **SCALES[args.scale])
        t0 = time.perf_counter()
        res = run_full(world / "erp", MAPPING, REPO / "config" / "rules.yaml", tmp / "out")
        elapsed = time.perf_counter() - t0
        lines = res.pipeline.ledger.height

    result: dict[str, Any] = {
        "scale": args.scale,
        "seed": args.seed,
        "lines": lines,
        "seconds": round(elapsed, 2),
        "machine": f"{platform.machine().replace('_', '-')} ({platform.system()})",
        "measures_section_8_target": args.scale == "full",
        "target_seconds": TARGET_SECONDS,
        "target_min_lines": TARGET_MIN_LINES,
    }
    if args.scale != "full":
        result["note"] = (
            "Reduced scale. This does not measure the section 8 performance target; "
            "the full measurement runs in the benchmark CI job."
        )
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
