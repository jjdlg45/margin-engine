"""Typer CLI entry point. Subcommands grow with each phase."""

from __future__ import annotations

from pathlib import Path

import typer

from margin_engine import __version__
from margin_engine.config import load_rules

app = typer.Typer(
    name="margin-engine",
    help="Auditable pricing and margin decision engine for B2B distributors.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the engine version."""
    typer.echo(__version__)


@app.command()
def validate_config(
    rules: Path = typer.Option(  # noqa: B008
        Path("config/rules.yaml"), "--rules", help="Path to rules.yaml"
    ),
) -> None:
    """Validate rules.yaml against the config schema."""
    loaded = load_rules(rules)
    typer.echo(f"OK: {rules} is valid (schema version {loaded.schema_.version})")


if __name__ == "__main__":
    app()
