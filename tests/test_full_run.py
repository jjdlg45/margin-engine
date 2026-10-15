"""Phase 8 verification: end-to-end run, report renders, manifest reproducible,
outputs written in both formats.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.report.full_run import FullRunResult, run_full

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


@pytest.fixture(scope="module")
def full_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, FullRunResult]:
    tmp = tmp_path_factory.mktemp("fullrun")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=6, n_customers=150, n_items=300)
    generate(cfg, tmp / "world")
    # Project rules with fewer bootstrap resamples for test runtime.
    rules = yaml.safe_load((REPO / "config" / "rules.yaml").read_text(encoding="utf-8"))
    rules["segmentation"]["bootstrap_resamples"] = 3
    rules_path = tmp / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rules), encoding="utf-8")
    out = tmp / "output"
    result = run_full(tmp / "world" / "erp", MAPPING, rules_path, out)
    return out, result


def test_run_completes_and_reconciles(full_run: tuple[Path, FullRunResult]) -> None:
    _, result = full_run
    assert result.pipeline.reconciliation.all_pass
    assert result.timings
    assert set(result.timings) >= {
        "ingest_classify_waterfall_reconcile",
        "segmentation",
        "sensitivity",
        "opportunity",
        "exports",
    }


def test_report_renders(full_run: tuple[Path, FullRunResult]) -> None:
    out, _ = full_run
    html = (out / "report.html").read_text(encoding="utf-8")
    for needle in (
        "Margin Engine Report",
        "Reconciliation check",
        "Pocket revenue",
        "Evidence status",
        "provisional_historical",
        "never summed",
    ):
        assert needle in html, needle


def test_manifest_complete(full_run: tuple[Path, FullRunResult]) -> None:
    out, _ = full_run
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["rules_sha256"] and manifest["mapping_sha256"]
    assert manifest["input_files"] and all("sha256" in f for f in manifest["input_files"])
    assert manifest["software_versions"]["polars"] != "absent"
    assert "verified_duplicates_removed" in manifest["cleaning_decisions"]
    assert manifest["stage_timings_s"]


def test_outputs_written_both_formats(full_run: tuple[Path, FullRunResult]) -> None:
    out, _ = full_run
    for name in ("financial_ledger", "guidance", "sensitivity", "opportunities", "leakage"):
        assert (out / f"{name}.parquet").exists()
        assert (out / f"{name}.csv").exists()
    assert (out / "engine.duckdb").exists()


def test_csv_money_columns_in_dollars(full_run: tuple[Path, FullRunResult]) -> None:
    import polars as pl

    out, _ = full_run
    df = pl.read_csv(out / "leakage.csv")
    assert any(c.endswith("_usd") for c in df.columns)
    assert not any(c.endswith("_cents") for c in df.columns)


def test_committed_sample_runs_via_cli(tmp_path: Path) -> None:
    """The README demo command must work on the committed sample dataset, so
    data/sample can never drift from the current mapping or generator."""
    from typer.testing import CliRunner

    from margin_engine.cli import app

    sample = REPO / "data" / "sample" / "erp"
    assert sample.exists(), "committed sample dataset missing"
    rules = yaml.safe_load((REPO / "config" / "rules.yaml").read_text(encoding="utf-8"))
    rules["segmentation"]["bootstrap_resamples"] = 2
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rules), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "--data",
            str(sample),
            "--mapping",
            str(MAPPING),
            "--rules",
            str(rules_path),
            "--out",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Reconciliation: PASS" in result.output
    assert (tmp_path / "out" / "report.html").exists()
