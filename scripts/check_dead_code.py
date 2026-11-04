"""Dead-code gate (Run 8): fail the build on a function, class, method or
property that nothing reaches.

Why a wrapper rather than plain `vulture`:

1. **It scans src/, api/, synth/ and scripts/, never tests/.** The Run 8
   wiring audit's whole subject was six functions that had tests and no
   caller, so a gate that counted test usage as life would have passed every
   one of them. Running vulture over tests/ as well reduces this to a single
   finding; that is the blind spot, not the good news.

2. **It reports only definitions, not variables and attributes.** At the
   confidence vulture assigns, this repo's ~580 "unused variable" and "unused
   attribute" hits are almost entirely pydantic and dataclass field
   declarations, which vulture cannot trace through model construction.
   Gating on those would mean an allowlist longer than the code, and the
   signal we want - a function nobody calls - would drown in it. Field-level
   dead data is left to review.

Framework-registered code is excluded by decorator (FastAPI routes and
exception handlers, Typer commands, pydantic validators). Everything else
that is genuinely unreachable belongs in .vulture_allowlist.py with a reason.

Exit codes: 0 clean, 1 findings, 2 vulture could not run.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCAN = ["src", "api", "synth", "scripts"]
ALLOWLIST = ".vulture_allowlist.py"
IGNORE_DECORATORS = "@router.*,@app.*,@model_validator*,@field_validator*"
# Definitions only. See the module docstring for why variables and attributes
# are reported but not gated.
GATED = re.compile(r"unused (function|class|method|property) ")


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "vulture",
        *SCAN,
        ALLOWLIST,
        "--min-confidence",
        "60",
        "--ignore-decorators",
        IGNORE_DECORATORS,
    ]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if proc.returncode not in (0, 3):  # 3 = vulture found something
        sys.stderr.write(proc.stderr or "vulture did not run\n")
        return 2

    findings = [ln for ln in proc.stdout.splitlines() if GATED.search(ln)]
    if not findings:
        print("dead-code gate: no unreachable functions, classes or methods")
        return 0

    print(f"dead-code gate: {len(findings)} unreachable definition(s)\n")
    for line in findings:
        print(f"  {line}")
    print(
        "\nEach one is a decision, not a formality:\n"
        "  - wire it into the pipeline, with a test that proves its output\n"
        "    reaches an export or the bundle; or\n"
        "  - delete it, and correct every doc that describes it as running; or\n"
        "  - add it to .vulture_allowlist.py with the reason it is unreachable\n"
        "    and stays. If the docs describe it as active, say 'not wired in'\n"
        "    where a reader will see it - tests/test_docs_match_code.py checks."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
