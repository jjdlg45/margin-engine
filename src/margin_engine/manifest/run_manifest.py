"""Run manifest (Phase 8): everything needed to reproduce a result months later.

Config hash, mapping hash, input file fingerprints, software versions, random
seed (where applicable), cleaning decisions and parameters.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import polars as pl

TRACKED_PACKAGES = [
    "polars",
    "duckdb",
    "pydantic",
    "scikit-learn",
    "pyfixest",
    "statsmodels",
    "numpy",
    "jinja2",
    "typer",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint_inputs(data_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(data_dir.glob("*.csv")):
        out.append({"file": p.name, "bytes": p.stat().st_size, "sha256": _sha256(p)})
    return out


def build_manifest(
    *,
    data_dir: Path,
    mapping_path: Path,
    rules_path: Path,
    cleaning_decisions: dict[str, Any],
    parameters: dict[str, Any],
    stage_timings_s: dict[str, float],
    seed: int | None = None,
) -> dict[str, Any]:
    versions = {"python": sys.version.split()[0], "platform": platform.platform()}
    for pkg in TRACKED_PACKAGES:
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            versions[pkg] = "absent"
    return {
        "created_utc": datetime.now(UTC).isoformat(),
        "rules_file": str(rules_path),
        "rules_sha256": _sha256(rules_path),
        "mapping_file": str(mapping_path),
        "mapping_sha256": _sha256(mapping_path),
        "input_files": fingerprint_inputs(data_dir),
        "software_versions": versions,
        "seed": seed,
        "cleaning_decisions": cleaning_decisions,
        "parameters": parameters,
        "stage_timings_s": {k: round(v, 3) for k, v in stage_timings_s.items()},
    }


def write_manifest(manifest: dict[str, Any], out_dir: Path) -> Path:
    path = out_dir / "run_manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def cleaning_decisions_from(
    ledger: pl.DataFrame,
    removed_duplicates: pl.DataFrame,
    blank_rows_dropped: dict[str, int] | None = None,
) -> dict[str, Any]:
    def count(col: str) -> int:
        return int(ledger[col].sum()) if col in ledger.columns else 0

    return {
        "blank_rows_dropped": sum((blank_rows_dropped or {}).values()),
        "blank_rows_dropped_by_file": dict(blank_rows_dropped or {}),
        "verified_duplicates_removed": removed_duplicates.height,
        "lookalike_duplicates_flagged_kept": count("flag_lookalike_duplicate"),
        "key_collisions_flagged_kept": count("flag_key_collision"),
        "missing_cost_lines": count("flag_missing_cost"),
        "zero_cost_lines_flagged": count("flag_zero_cost"),
        "zero_quantity_lines": count("flag_zero_quantity"),
        "missing_list_price_lines": count("flag_missing_list_price"),
    }
