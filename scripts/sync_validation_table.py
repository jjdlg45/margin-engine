"""Write the measured validation results into the README table.

    uv run python scripts/sync_validation_table.py

Run it after `scripts/measure_validation.py`. The companion test
`tests/test_validation_table.py` fails if this was not run.
"""

from __future__ import annotations

from pathlib import Path

from margin_engine.validation_table import load, render, splice

REPO = Path(__file__).resolve().parents[1]


def main() -> None:
    readme_path = REPO / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    block = render(load(REPO / "docs" / "validation.json"))
    updated = splice(readme, block)
    if updated == readme:
        print("README validation table already matches docs/validation.json")
        return
    readme_path.write_text(updated, encoding="utf-8", newline="\n")
    print("README validation table updated from docs/validation.json")


if __name__ == "__main__":
    main()
