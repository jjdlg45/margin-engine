"""Phase 35: real-data validation is behind --validate and the validate command.

The default `run` skips the suite (it dominated runtime); `run --validate` and
`margin-engine validate` include it. Library callers keep the config switch.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from margin_engine.cli import app

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample" / "erp"
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"
RULES = REPO / "config" / "rules.yaml"

VALIDATION_FILES = [
    "real_validation_backtest.csv",
    "real_validation_placebo.csv",
    "real_validation_stability_elasticity.csv",
    "real_validation_stability_segments.csv",
]


def _run(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output


def test_run_default_skips_validation(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(
        [
            "run",
            "--data",
            str(SAMPLE),
            "--mapping",
            str(MAPPING),
            "--rules",
            str(RULES),
            "--out",
            str(out),
        ]
    )
    for name in VALIDATION_FILES:
        assert not (out / name).exists(), f"{name} produced without --validate"
    # The skip is stated, never silent.
    manifest = (out / "run_manifest.json").read_text(encoding="utf-8")
    assert "real-data validation OFF" in manifest


def test_validate_command_produces_suite(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(
        [
            "validate",
            "--data",
            str(SAMPLE),
            "--mapping",
            str(MAPPING),
            "--rules",
            str(RULES),
            "--out",
            str(out),
        ]
    )
    for name in VALIDATION_FILES:
        assert (out / name).exists(), f"{name} missing from the validate command"
