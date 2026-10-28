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
    MissingScopeError,
    _cell,
    check_meta,
    current_block,
    load,
    load_meta,
    render,
    render_detail,
    splice,
)

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
RESULTS = REPO / "docs" / "validation.json"
META = REPO / "docs" / "validation-meta.yaml"
DETAIL = REPO / "docs" / "validation.md"


def _meta_for(doc: dict) -> dict:
    """Scope entries for a hand-built results document."""
    return {
        r["check"]: {
            "worlds": 1,
            "method": "m",
            "establishes": "e",
            "does_not_establish": "d",
        }
        for r in doc["results"]
    }


def test_readme_table_matches_the_measured_results_file() -> None:
    readme = README.read_text(encoding="utf-8")
    expected = render(load(RESULTS), load_meta(META))
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
    out = render(doc, _meta_for(doc))
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
    row = next(ln for ln in render(doc, _meta_for(doc)).splitlines() if ln.startswith("| a "))
    # Five structural separators now that the table carries a Worlds column.
    assert row.count("|") - row.count(r"\|") == 5


def test_splice_replaces_only_the_block() -> None:
    doc = load(RESULTS)
    before = f"head\n\n{START}\nstale\n{END}\n\ntail\n"
    after = splice(before, render(doc, load_meta(META)))
    assert after.startswith("head\n\n") and after.endswith("\n\ntail\n")
    assert "stale" not in after


def test_a_readme_without_markers_fails_loudly() -> None:
    with pytest.raises(ValueError):
        current_block("no markers here")


def test_every_measured_check_declares_its_scope() -> None:
    """A number without its scope is the thing Finding 22 objected to."""
    check_meta(load(RESULTS), load_meta(META))


def test_a_check_without_scope_is_refused() -> None:
    doc = {
        "generated_utc": "x",
        "script": "s",
        "runtime_s": 1.0,
        "results": [{"check": "brand new", "target": "t", "achieved": "a", "passed": True}],
    }
    with pytest.raises(MissingScopeError, match="brand new"):
        render(doc, {})


def test_scope_that_describes_nothing_measured_is_refused() -> None:
    doc = {
        "generated_utc": "x",
        "script": "s",
        "runtime_s": 1.0,
        "results": [{"check": "a", "target": "t", "achieved": "m", "passed": True}],
    }
    meta = _meta_for(doc)
    meta["a check nobody runs"] = dict(meta["a"])
    with pytest.raises(MissingScopeError, match="no longer measures"):
        render(doc, meta)


def test_an_empty_scope_field_is_refused() -> None:
    doc = {
        "generated_utc": "x",
        "script": "s",
        "runtime_s": 1.0,
        "results": [{"check": "a", "target": "t", "achieved": "m", "passed": True}],
    }
    meta = _meta_for(doc)
    meta["a"]["does_not_establish"] = "   "
    with pytest.raises(MissingScopeError, match="does_not_establish"):
        render(doc, meta)


def test_the_detail_document_matches_the_measurements() -> None:
    expected = render_detail(load(RESULTS), load_meta(META))
    assert DETAIL.read_text(encoding="utf-8") == expected, (
        "docs/validation.md is out of date. Run: uv run python scripts/sync_validation_table.py"
    )


def test_every_row_states_what_it_does_not_establish() -> None:
    text = DETAIL.read_text(encoding="utf-8")
    rows = load(RESULTS)["results"]
    assert text.count("- **Does not establish**") == len(rows)
    assert text.count("- **Worlds or clusters**") == len(rows)
    # A gap is named as a gap in the detail document too.
    for r in rows:
        if not r["passed"]:
            assert "KNOWN GAP - not met" in text
