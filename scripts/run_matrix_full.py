"""Run 4 (27.7): full-tier scenario matrix. Several hundred generated worlds,
every invariant checked on each, summary recorded in docs/validation.json
(worlds run, invariant pass counts, any failures with the world's parameters
so they can be reproduced).

Usage:
    uv run python scripts/run_matrix_full.py            # run + update row
    uv run python scripts/run_matrix_full.py --dry      # run only, no write
    uv run python scripts/run_matrix_full.py --n 50     # smaller probe run
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.matrix_worlds import sample_worlds  # noqa: E402

N_FULL = 300
FULL_SIZES = ((20, 40), (30, 60), (50, 80), (80, 120), (120, 200))
CHECK_NAME = "Scenario matrix (full tier)"
TARGET = "every invariant holds on every world (reconciliation, fail-closed, determinism, format invariance, monotonicity, no crash)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="do not update validation.json")
    ap.add_argument("--n", type=int, default=N_FULL, help="number of worlds")
    args = ap.parse_args()

    from tests.matrix_worlds import run_world

    specs = sample_worlds(args.n, FULL_SIZES, matrix_seed=20278)
    t0 = time.perf_counter()
    failures: list[dict[str, object]] = []
    n_crash = 0
    for i, spec in enumerate(specs):
        with tempfile.TemporaryDirectory() as td:
            try:
                result = run_world(spec, Path(td))
            except Exception as exc:
                n_crash += 1
                failures.append(
                    {"spec": {**spec.__dict__, "preset": spec.preset.value}, "crash": repr(exc)}
                )
                print(f"[{i + 1}/{len(specs)}] {spec.label()}: CRASH {exc!r}", flush=True)
                continue
            if result["failures"]:
                failures.append(result)
                print(
                    f"[{i + 1}/{len(specs)}] {spec.label()}: FAIL {result['failures']}", flush=True
                )
            elif (i + 1) % 10 == 0:
                print(f"[{i + 1}/{len(specs)}] ok (latest: {spec.label()})", flush=True)
    elapsed = time.perf_counter() - t0

    n_pass = len(specs) - len(failures)
    passed = not failures
    achieved = (
        f"{len(specs)} worlds, {n_pass} passed all invariants, "
        f"{len(failures)} failed ({n_crash} crashes), {elapsed:.0f}s"
    )
    print(f"\n{achieved}")
    if failures:
        print(json.dumps(failures[:10], indent=1, default=str))

    if not args.dry:
        out_path = REPO / "docs" / "validation.json"
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        row = {
            "check": CHECK_NAME,
            "target": TARGET,
            "achieved": achieved,
            "passed": passed,
            **({"failures": failures} if failures else {}),
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
