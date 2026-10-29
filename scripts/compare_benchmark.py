"""Compare two benchmark runs and decide whether the branch may merge.

Two independent bars, because each catches what the other cannot:

* **Absolute.** The branch must meet section 8: 500k+ lines under 2 minutes.
* **Relative.** The branch must not be more than `--max-regression-pct` slower
  than `main` measured on the SAME runner. An absolute bar alone cannot tell a
  real regression from a slower runner, and a relative bar alone would let the
  pipeline drift past two minutes a few per cent at a time.

    python scripts/compare_benchmark.py --base base.json --head head.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SUMMARY_HEADER = "## Run-1 pipeline benchmark\n\n"


def _load(p: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path, required=True, help="benchmark JSON from main")
    ap.add_argument("--head", type=Path, required=True, help="benchmark JSON from the branch")
    ap.add_argument("--max-regression-pct", type=float, default=10.0)
    ap.add_argument("--summary", type=Path, help="append a markdown summary here")
    args = ap.parse_args()

    base, head = _load(args.base), _load(args.head)
    target_s = float(head["target_seconds"])
    min_lines = int(head["target_min_lines"])
    change_pct = 100.0 * (head["seconds"] - base["seconds"]) / base["seconds"]

    failures: list[str] = []
    if not head["measures_section_8_target"]:
        failures.append("the branch result is a reduced-scale run and cannot gate the target")
    if head["lines"] < min_lines:
        failures.append(f"only {head['lines']:,} lines, under the {min_lines:,} the target states")
    if head["seconds"] >= target_s:
        failures.append(f"{head['seconds']:.1f}s is over the {target_s:.0f}s bar")
    if change_pct > args.max_regression_pct:
        failures.append(
            f"{change_pct:+.1f}% against main, over the {args.max_regression_pct:.0f}% allowed"
        )

    lines = [
        SUMMARY_HEADER,
        "| | lines | seconds |\n|---|---|---|\n",
        f"| main | {base['lines']:,} | {base['seconds']:.1f} |\n",
        f"| this branch | {head['lines']:,} | {head['seconds']:.1f} |\n",
        f"\nChange against main on the same runner: **{change_pct:+.1f}%** "
        f"(allowed: {args.max_regression_pct:.0f}%). "
        f"Bar: {min_lines:,}+ lines under {target_s:.0f}s.\n",
    ]
    if failures:
        lines.append("\n**Failed:**\n" + "".join(f"- {f}\n" for f in failures))
    else:
        lines.append("\nWithin both the absolute bar and the allowed change against main.\n")
    report = "".join(lines)
    print(report)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(report)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
