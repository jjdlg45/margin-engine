"""Render the README validation table from the measured results file.

`scripts/measure_validation.py` runs the measurements and writes
`docs/validation.json`. The README table is generated from that file rather
than typed, because a typed table drifts: between Run 5 and Run 6 the
committed README still showed a 37.0s pipeline time against a measured
119.0s, and a 92.0% coverage figure against a measured 94.7%, while claiming
"no number is hand-typed".

`tests/test_validation_table.py` re-renders this block and fails when the
committed README differs from the measurements, so the claim is enforced
rather than asserted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

START = "<!-- validation-table:start -->"
END = "<!-- validation-table:end -->"

#: Prefix on the measured cell of any row whose target was not met, so a gap
#: reads as a gap in the README exactly as it does in validation.json.
GAP_PREFIX = "**KNOWN GAP.** "


def load(path: Path) -> dict[str, Any]:
    """Read a validation results document."""
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def _cell(text: str) -> str:
    """Make a string safe inside a markdown table cell."""
    return str(text).replace("|", r"\|").replace("\n", " ").strip()


def render(doc: dict[str, Any]) -> str:
    """Render the full README block, markers included."""
    runtime = float(doc.get("runtime_s") or 0.0)
    lines = [
        START,
        "",
        (
            f"Measured {doc['generated_utc']} by `{doc['script']}` "
            f"in {runtime / 60:,.0f} minutes. This table is generated from "
            "`docs/validation.json`; a test fails if the two disagree."
        ),
        "",
        "| Check | Target | Measured |",
        "|---|---|---|",
    ]
    for row in doc["results"]:
        achieved = _cell(row["achieved"])
        if not row["passed"] and "KNOWN GAP" not in achieved:
            achieved = GAP_PREFIX + achieved
        lines.append(f"| {_cell(row['check'])} | {_cell(row['target'])} | {achieved} |")
    lines += ["", END]
    return "\n".join(lines)


def splice(readme: str, block: str) -> str:
    """Replace the marked block in `readme` with `block`."""
    start = readme.index(START)
    end = readme.index(END) + len(END)
    return readme[:start] + block + readme[end:]


def current_block(readme: str) -> str:
    """Return the marked block as it stands in `readme`."""
    start = readme.index(START)
    end = readme.index(END) + len(END)
    return readme[start:end]
