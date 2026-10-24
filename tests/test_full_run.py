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
        # The provisional-target statement renders through the label
        # dictionary: the raw code never reaches a reader.
        "provisional (historical)",
        "never summed",
    ):
        assert needle in html, needle
    assert "provisional_historical" not in html
    # Money renders as dollars with cents in every table; no raw-cents headers.
    assert "(cents)" not in html
    import re as _re

    assert _re.search(r"\$\d{1,3}(,\d{3})*\.\d{2}", html), "no $x,xxx.xx amounts found"
    # Residuals, when present, carry plain-English explanations.
    if "Visible residuals" in html:
        assert "Why it is not allocated to lines" in html


def test_manifest_complete(full_run: tuple[Path, FullRunResult]) -> None:
    out, _ = full_run
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["rules_sha256"] and manifest["mapping_sha256"]
    assert manifest["input_files"] and all("sha256" in f for f in manifest["input_files"])
    assert manifest["software_versions"]["polars"] != "absent"
    assert "verified_duplicates_removed" in manifest["cleaning_decisions"]
    assert manifest["stage_timings_s"]
    # Library warnings are captured into the manifest, not printed (17.4).
    assert "library_warnings" in manifest["parameters"]
    for w in manifest["parameters"]["library_warnings"]:
        assert w["category"] and w["message"] and w["n_occurrences"] >= 1


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


def test_cli_sample_produces_optional_module_outputs(tmp_path: Path) -> None:
    """The committed sample carries receipts, quotes and substitution groups,
    so the CLI run must produce every optional module's output files."""
    from typer.testing import CliRunner

    from margin_engine.cli import app

    sample = REPO / "data" / "sample" / "erp"
    rules = yaml.safe_load((REPO / "config" / "rules.yaml").read_text(encoding="utf-8"))
    rules["segmentation"]["bootstrap_resamples"] = 2
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rules), encoding="utf-8")
    out = tmp_path / "out"
    result = CliRunner().invoke(
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
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    for name in (
        "inventory_cogs",
        "inventory_identity",
        "win_curves",
        "cross_price_estimates",
        "unallocated_settlements",
    ):
        assert (out / f"{name}.csv").exists(), name
        assert (out / f"{name}.parquet").exists(), name
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "Inventory costing" in html
    assert "Quote win/loss curves" in html
    assert "Cross-price effects" in html
    # unallocated_settlements has headers even when empty.
    first_line = (out / "unallocated_settlements.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "customer_id" in first_line


def test_report_has_economics_section(full_run: tuple[Path, FullRunResult]) -> None:
    out, _ = full_run
    html = (out / "report.html").read_text(encoding="utf-8")
    for needle in (
        "Order and customer economics",
        "Top contributors (after activity cost)",
        "Bottom contributors (after activity cost)",
        "excluded from contribution because cost is unknown",
    ):
        assert needle in html, needle


def _assert_cli_twice_identical(data_dir: Path, tmp_path: Path) -> None:
    """Run the full CLI twice on data_dir and assert every CSV and Parquet
    export is byte-identical. The manifest carries a timestamp and the report
    embeds it, so those and the DuckDB store are excluded."""
    import hashlib

    from typer.testing import CliRunner

    from margin_engine.cli import app

    rules = yaml.safe_load((REPO / "config" / "rules.yaml").read_text(encoding="utf-8"))
    rules["segmentation"]["bootstrap_resamples"] = 2
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rules), encoding="utf-8")

    def run(out: Path) -> dict[str, str]:
        result = CliRunner().invoke(
            app,
            [
                "run",
                "--data",
                str(data_dir),
                "--mapping",
                str(MAPPING),
                "--rules",
                str(rules_path),
                "--out",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(out.iterdir())
            if p.suffix in (".csv", ".parquet")
        }

    h1 = run(tmp_path / "out1")
    h2 = run(tmp_path / "out2")
    assert set(h1) == set(h2)
    assert len(h1) >= 15
    diff = [name for name in h1 if h1[name] != h2[name]]
    assert not diff, f"exports differ across identical runs: {diff}"


def test_cli_twice_byte_identical_exports(tmp_path: Path) -> None:
    """Engine determinism on the committed sample dataset."""
    _assert_cli_twice_identical(REPO / "data" / "sample" / "erp", tmp_path)


def test_cli_twice_byte_identical_full_size_world(tmp_path: Path) -> None:
    """Engine determinism at full scale: a default-size generated world
    (seed 42), where solver iteration counts, group cardinalities and float
    magnitudes all differ from the small committed sample."""
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=42)
    generate(cfg, tmp_path / "world")
    _assert_cli_twice_identical(tmp_path / "world" / "erp", tmp_path)
