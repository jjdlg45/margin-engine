"""The merge gate on pipeline speed.

A pull request failed on a bar that had nothing to do with it: main measured
187.9s on a GitHub runner, the branch 191.6s - 2.0% slower, well inside the
10% gate - and both tripped a 120s absolute bar calibrated on a 14 GB laptop
where the same code runs in 119.0s. The absolute number does not transfer
between machines; the comparison of two runs on one machine does.

So the relative comparison is the gate, and the absolute bar is a per-machine
backstop passed in by whoever knows the machine. These tests hold both halves
of that, including the exact numbers from the run that prompted it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "compare_benchmark.py"

# As measured on a GitHub ubuntu-24.04 runner, 2026-10-29.
RUNNER_MAIN = 187.9
RUNNER_BRANCH = 191.6
RUNNER_BAR = 250.0
# As measured on the 14 GB development laptop (median of three).
LAPTOP_SECONDS = 119.0
LAPTOP_BAR = 120.0


def _bench(tmp: Path, name: str, seconds: float, *, lines: int = 811_253, full: bool = True):
    p = tmp / f"{name}.json"
    p.write_text(
        json.dumps(
            {
                "seconds": seconds,
                "lines": lines,
                "measures_section_8_target": full,
                "target_seconds": LAPTOP_BAR,
                "target_min_lines": 500_000,
            }
        ),
        encoding="utf-8",
    )
    return p


def _run(tmp: Path, base_s: float, head_s: float, *extra: str, **kw: Any):
    base = _bench(tmp, "base", base_s)
    head = _bench(tmp, "head", head_s, **kw)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", str(base), "--head", str(head), *extra],
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stdout


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_a_real_regression_against_main_fails(tmp_path: Path) -> None:
    rc, out = _run(tmp_path, 100.0, 115.0, "--max-seconds", "250")
    assert rc == 1
    assert "+15.0% against main on the same machine" in out


def test_a_change_inside_the_gate_passes(tmp_path: Path) -> None:
    rc, out = _run(tmp_path, 100.0, 105.0, "--max-seconds", "250")
    assert rc == 0
    assert "+5.0%" in out


def test_the_gate_is_reported_as_the_gate(tmp_path: Path) -> None:
    """A reader of the summary has to be able to tell which number decides."""
    _, out = _run(tmp_path, 100.0, 102.0, "--max-seconds", "250")
    assert "Gate - change against main on the same machine" in out
    assert "Backstop" in out


def test_the_allowed_regression_is_configurable(tmp_path: Path) -> None:
    rc, _ = _run(tmp_path, 100.0, 105.0, "--max-seconds", "250", "--max-regression-pct", "2")
    assert rc == 1


# ---------------------------------------------------------------------------
# The per-machine backstop
# ---------------------------------------------------------------------------


def test_the_run_that_prompted_this_now_passes(tmp_path: Path) -> None:
    """187.9s against 191.6s on one runner: 2.0% apart, both far over a laptop
    bar and both inside the runner's."""
    rc, out = _run(
        tmp_path,
        RUNNER_MAIN,
        RUNNER_BRANCH,
        "--max-seconds",
        str(RUNNER_BAR),
        "--bar-label",
        "the GitHub ubuntu-24.04 runner",
    )
    assert rc == 0, out
    assert "+2.0%" in out
    assert "the GitHub ubuntu-24.04 runner" in out


def test_the_same_run_failed_on_the_laptop_bar(tmp_path: Path) -> None:
    """The behaviour being removed, kept as a test so the reason stays legible:
    with the laptop's bar the same runner numbers fail on the absolute check
    while the gate is satisfied."""
    rc, out = _run(tmp_path, RUNNER_MAIN, RUNNER_BRANCH, "--max-seconds", str(LAPTOP_BAR))
    assert rc == 1
    assert "over the 120s bar" in out
    assert "+2.0% against main" not in out.split("**Failed:**")[1]


def test_a_blow_up_still_fails_the_backstop(tmp_path: Path) -> None:
    """The backstop exists so the pipeline cannot drift past a wall a few per
    cent at a time: main itself slow, branch barely slower, still refused."""
    rc, out = _run(tmp_path, 260.0, 262.0, "--max-seconds", str(RUNNER_BAR))
    assert rc == 1
    assert "over the 250s bar" in out


def test_without_a_bar_it_falls_back_to_the_measured_target(tmp_path: Path) -> None:
    """Run locally with no machine stated, the script holds the laptop figure
    recorded in the benchmark output rather than inventing one."""
    # +5.2% against main, so the gate is satisfied; 121s is over the laptop
    # figure the output carries, so the backstop is what refuses it.
    rc, out = _run(tmp_path, 115.0, 121.0)
    assert rc == 1
    assert "over the 120s bar for this machine" in out

    rc, _ = _run(tmp_path, 115.0, LAPTOP_SECONDS - 1.0)
    assert rc == 0


def test_the_summary_says_absolute_timings_do_not_transfer(tmp_path: Path) -> None:
    _, out = _run(tmp_path, RUNNER_MAIN, RUNNER_BRANCH, "--max-seconds", str(RUNNER_BAR))
    assert "do not transfer between machines" in out
    assert "was measured on the development laptop" in out


# ---------------------------------------------------------------------------
# A measurement that cannot gate anything
# ---------------------------------------------------------------------------


def test_a_reduced_scale_run_cannot_gate(tmp_path: Path) -> None:
    rc, out = _run(tmp_path, 100.0, 100.0, "--max-seconds", "250", full=False)
    assert rc == 1
    assert "reduced-scale run" in out


def test_too_few_lines_cannot_gate(tmp_path: Path) -> None:
    rc, out = _run(tmp_path, 100.0, 100.0, "--max-seconds", "250", lines=400_000)
    assert rc == 1
    assert "under the 500,000" in out
