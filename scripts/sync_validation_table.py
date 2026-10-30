"""Write the measured validation results into the README and docs/validation.md.

    uv run python scripts/sync_validation_table.py

Run it after `scripts/measure_validation.py`. The companion test
`tests/test_validation_table.py` fails if this was not run.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Run as `python scripts/sync_validation_table.py`, only `scripts/` is on the
# path, so the sibling import below needs the repo root put there first.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.measure_validation import declared_checks  # noqa: E402

from margin_engine.validation_table import (  # noqa: E402
    load,
    load_meta,
    render,
    render_detail,
    splice,
)


def main() -> None:
    doc = load(REPO / "docs" / "validation.json")
    meta = load_meta(REPO / "docs" / "validation-meta.yaml")
    # Scope entries may describe a row the measurement scripts can produce but
    # the committed results file has not caught up with yet. That is not an
    # orphan: the alternative is a two-hour study ending with nowhere to say
    # what its newest row establishes.
    declared = declared_checks()

    readme_path = REPO / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    updated = splice(readme, render(doc, meta, declared))
    if updated == readme:
        print("README validation table already matches docs/validation.json")
    else:
        readme_path.write_text(updated, encoding="utf-8", newline="\n")
        print("README validation table updated from docs/validation.json")

    detail_path = REPO / "docs" / "validation.md"
    detail = render_detail(doc, meta, declared)
    if detail_path.exists() and detail_path.read_text(encoding="utf-8") == detail:
        print("docs/validation.md already matches")
    else:
        detail_path.write_text(detail, encoding="utf-8", newline="\n")
        print("docs/validation.md updated")


if __name__ == "__main__":
    main()
