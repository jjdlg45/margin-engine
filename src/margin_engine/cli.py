"""Typer CLI entry point. Subcommands grow with each phase.

Data and configuration errors (mapping semantics, unparseable money, schema
violations, reconciliation failures) exit with code 1 and a clean actionable
message — including up to 10 offending source line keys where the error is
line-specific — instead of a traceback.
"""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import ValidationError

from margin_engine import __version__
from margin_engine.config import load_rules
from margin_engine.ingest.mapping import MappingError
from margin_engine.money import MoneyParseError
from margin_engine.waterfall.reconcile import ReconciliationError

app = typer.Typer(
    name="margin-engine",
    help="Auditable pricing and margin decision engine for B2B distributors.",
    no_args_is_help=True,
)


def _fail(kind: str, exc: Exception) -> None:
    typer.echo(f"ERROR ({kind}): {exc}", err=True)
    raise typer.Exit(code=1) from exc


@app.command()
def version() -> None:
    """Print the engine version."""
    typer.echo(__version__)


@app.command()
def run(
    data: Path = typer.Option(..., "--data", help="Directory with the source export"),  # noqa: B008
    mapping: Path = typer.Option(..., "--mapping", help="Mapping YAML for the source"),  # noqa: B008
    rules: Path = typer.Option(  # noqa: B008
        Path("config/rules.yaml"), "--rules", help="Policy rules YAML"
    ),
    out: Path = typer.Option(Path("output"), "--out", help="Output directory"),  # noqa: B008
) -> None:
    """Run the full pipeline: ingest -> waterfall -> analytics -> report."""
    from margin_engine.report.full_run import run_full

    try:
        result = run_full(data, mapping, rules, out)
    except MappingError as exc:
        _fail("mapping", exc)
    except MoneyParseError as exc:
        _fail("money parsing", exc)
    except ValidationError as exc:
        _fail("configuration", exc)
    except ReconciliationError as exc:
        _fail("reconciliation", exc)
    typer.echo(f"Ledger lines: {result.pipeline.ledger.height}")
    typer.echo(f"Reconciliation: {'PASS' if result.pipeline.reconciliation.all_pass else 'FAIL'}")
    for stage, secs in result.timings.items():
        typer.echo(f"  {stage}: {secs:.1f}s")
    typer.echo(f"Report: {out / 'report.html'}")
    typer.echo(f"Manifest: {out / 'run_manifest.json'}")


@app.command()
def validate_config(
    rules: Path = typer.Option(  # noqa: B008
        Path("config/rules.yaml"), "--rules", help="Path to rules.yaml"
    ),
) -> None:
    """Validate rules.yaml against the config schema."""
    try:
        loaded = load_rules(rules)
    except (ValidationError, ValueError, TypeError) as exc:
        _fail("configuration", exc)
    typer.echo(f"OK: {rules} is valid (schema version {loaded.schema_.version})")


if __name__ == "__main__":
    app()
