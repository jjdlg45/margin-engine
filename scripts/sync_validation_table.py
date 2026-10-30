"""Write the measured validation results into the README and docs/validation.md.

    uv run python scripts/sync_validation_table.py

Run it after `scripts/measure_validation.py`. The companion test
`tests/test_validation_table.py` fails if this was not run.
"""

from __future__ import annotations

from pathlib import Path

from scripts.measure_validation import declared_checks

from margin_engine.validation_table import load, load_meta, render, render_detail, splice

REPO = Path(__file__).resolve().parents[1]


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
