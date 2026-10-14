"""HTML report rendering (Phase 8). Every model output shows its evidence
status; every section states its data coverage. Jinja2, single file output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"


def render_report(context: dict[str, Any], out_path: Path) -> Path:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    tpl = env.get_template("report.html.j2")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(tpl.render(**context), encoding="utf-8")
    return out_path
