"""The README validation table must equal the measured results file.

Between Run 5 and Run 6 the committed README table drifted from
`docs/validation.json` while the paragraph above it claimed "no number is
hand-typed": it showed a 37.0s pipeline time against a measured 119.0s and
92.0% interval coverage against a measured 94.7%. The table is now generated,
and this test is what makes the claim true.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from margin_engine.validation_table import (
    END,
    GAP_PREFIX,
    START,
    _cell,
    current_block,
    load,
    render,
    splice,
)

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
RESULTS = REPO / "docs" / "validation.json"


def test_readme_table_matches_the_measured_results_file() -> None:
    readme = README.read_text(encoding="utf-8")
    expected = render(load(RESULTS))
    assert current_block(readme) == expected, (
        "README validation table is out of date. Run:\n"
        "    uv run python scripts/sync_validation_table.py"
    )


def test_every_measured_value_reaches_the_readme() -> None:
    """No row may be silently dropped on the way to the README."""
    readme = README.read_text(encoding="utf-8")
    block = current_block(readme)
    for row in load(RESULTS)["results"]:
        assert _cell(row["check"]) in block, row["check"]
        assert _cell(row["achieved"]) in block, row["check"]


def test_the_readme_states_when_the_measurements_were_taken() -> None:
    doc = load(RESULTS)
    assert doc["generated_utc"] in current_block(README.read_text(encoding="utf-8"))


def test_no_validation_figure_sits_outside_the_generated_block() -> None:
    """A second, hand-typed copy of the table is how the drift happened."""
    readme = README.read_text(encoding="utf-8")
    outside = readme.replace(current_block(readme), "")
    section = outside[outside.index("## Validation results") :]
    section = section[: section.index("## Method in brief")]
    assert "| Check |" not in section
    # Percentages and seconds are the shapes the stale figures took.
    assert not re.search(r"\d+\.\d+s\b", section), "a timing figure is typed outside the table"


def test_a_gap_is_labelled_once() -> None:
    doc = {
        "generated_utc": "x",
        "script": "s",
        "runtime_s": 1.0,
        "results": [
            {"check": "a", "target": "t", "achieved": "missed it", "passed": False},
            {"check": "b", "target": "t", "achieved": "KNOWN GAP: missed it", "passed": False},
            {"check": "c", "target": "t", "achieved": "fine", "passed": True},
        ],
    }
    out = render(doc)
    assert f"| a | t | {GAP_PREFIX}missed it |" in out
    assert "| b | t | KNOWN GAP: missed it |" in out
    assert "| c | t | fine |" in out


def test_a_pipe_in_a_measurement_cannot_break_the_table() -> None:
    doc = {
        "generated_utc": "x",
        "script": "s",
        "runtime_s": 1.0,
        "results": [{"check": "a", "target": "|x - y| <= 1", "achieved": "ok", "passed": True}],
    }
    row = next(ln for ln in render(doc).splitlines() if ln.startswith("| a "))
    assert row.count("|") - row.count(r"\|") == 4  # four structural separators


def test_splice_replaces_only_the_block() -> None:
    doc = load(RESULTS)
    before = f"head\n\n{START}\nstale\n{END}\n\ntail\n"
    after = splice(before, render(doc))
    assert after.startswith("head\n\n") and after.endswith("\n\ntail\n")
    assert "stale" not in after


def test_a_readme_without_markers_fails_loudly() -> None:
    with pytest.raises(ValueError):
        current_block("no markers here")
