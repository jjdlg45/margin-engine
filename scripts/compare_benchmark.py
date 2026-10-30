"""Compare two benchmark runs and decide whether the branch may merge.

**The gate is the relative comparison**: this branch against `main`, measured
on the SAME machine, back to back. That is the only comparison that can tell a
code regression from a slow machine, and it is what fails a pull request.

The absolute bar is a **second, machine-specific** check, kept because a
relative bar alone would let the pipeline drift past any wall a few per cent
at a time. It is not a universal number and must not be treated as one: the
same code measures 119.0s on the 14 GB development laptop (811,253 lines) and
187.9s on a GitHub `ubuntu-24.04` runner. A bar calibrated on one machine
fails on the other for no reason a reader could act on, which is exactly what
happened: a 120s bar set on the laptop failed a runner job whose branch was
2.0% slower than main - well inside the gate.

So `--max-seconds` is passed by whoever knows the machine. It defaults to the
`target_seconds` recorded in the benchmark output, which is the section 8
figure measured on the development laptop.

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
    ap.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help=(
            "absolute bar in seconds, calibrated for the machine this runs on. "
            "Defaults to the target_seconds in the benchmark output, which was "
            "measured on the development laptop and does not transfer."
        ),
    )
    ap.add_argument(
        "--bar-label",
        default="this machine",
        help="what the absolute bar is calibrated to, for the summary",
    )
    ap.add_argument("--summary", type=Path, help="append a markdown summary here")
    args = ap.parse_args()

    base, head = _load(args.base), _load(args.head)
    spec_s = float(head["target_seconds"])
    max_s = float(args.max_seconds) if args.max_seconds is not None else spec_s
    min_lines = int(head["target_min_lines"])
    change_pct = 100.0 * (head["seconds"] - base["seconds"]) / base["seconds"]

    # The gate first, because it is the one that means something: both sides
    # measured on this machine, minutes apart.
    failures: list[str] = []
    if change_pct > args.max_regression_pct:
        failures.append(
            f"{change_pct:+.1f}% against main on the same machine, over the "
            f"{args.max_regression_pct:.0f}% allowed"
        )
    if not head["measures_section_8_target"]:
        failures.append("the branch result is a reduced-scale run and cannot gate the target")
    if head["lines"] < min_lines:
        failures.append(f"only {head['lines']:,} lines, under the {min_lines:,} the target states")
    if head["seconds"] >= max_s:
        failures.append(f"{head['seconds']:.1f}s is over the {max_s:.0f}s bar for {args.bar_label}")

    lines = [
        SUMMARY_HEADER,
        "| | lines | seconds |\n|---|---|---|\n",
        f"| main | {base['lines']:,} | {base['seconds']:.1f} |\n",
        f"| this branch | {head['lines']:,} | {head['seconds']:.1f} |\n",
        f"\n**Gate - change against main on the same machine: {change_pct:+.1f}%** "
        f"(allowed: {args.max_regression_pct:.0f}%).\n"
        f"\nBackstop: {min_lines:,}+ lines under {max_s:.0f}s, the bar for "
        f"{args.bar_label}. Absolute timings do not transfer between machines - "
        f"the section 8 figure of {spec_s:.0f}s was measured on the development "
        f"laptop.\n",
    ]
    if failures:
        lines.append("\n**Failed:**\n" + "".join(f"- {f}\n" for f in failures))
    else:
        lines.append(
            f"\nWithin the {args.max_regression_pct:.0f}% gate against main, and "
            f"within the {max_s:.0f}s bar for {args.bar_label}.\n"
        )
    report = "".join(lines)
    print(report)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(report)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
