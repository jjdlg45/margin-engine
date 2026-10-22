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


# Demo world for the hosted site: generated in CI at deploy time, never
# committed. Scale chosen in Phase 19.1 as the smallest where at least one
# segment reaches causal-supported elasticity with the bundle under 5 MB.
# Smallest scanned world with >=1 causal-supported segment under the Run 4
# criteria (AR bounded and <= 1.5x the primary CI, carried-forward robustness
# gap, identifying-support guard): 50 x 120 at seed 3. The Phase 19.1 world
# (40 x 100 seed 42) no longer reaches causal with the stricter gates.
DEMO_SEED = 3
DEMO_CUSTOMERS = 50
DEMO_ITEMS = 120


@app.command()
def export_web(
    data: Path | None = typer.Option(None, "--data", help="Directory with the source export"),  # noqa: B008
    mapping: Path | None = typer.Option(None, "--mapping", help="Mapping YAML for the source"),  # noqa: B008
    rules: Path = typer.Option(  # noqa: B008
        Path("config/rules.yaml"), "--rules", help="Policy rules YAML"
    ),
    out: Path = typer.Option(  # noqa: B008
        Path("web/public/data"), "--out", help="Bundle output directory"
    ),
    demo: bool = typer.Option(
        False, "--demo", help="Generate the fixed-seed demo world and export it"
    ),
) -> None:
    """Run the pipeline and export the versioned JSON bundle for the hosted demo."""
    import tempfile

    from margin_engine.config import load_rules as _load_rules
    from margin_engine.report.full_run import run_full
    from margin_engine.webexport.build import build_bundle, write_bundle

    label: str | None = None
    try:
        with tempfile.TemporaryDirectory() as td:
            if demo:
                # synth/ lives at the repo root (dev/CI feature, editable install).
                import sys as _sys

                _repo = str(Path(__file__).resolve().parents[2])
                if _repo not in _sys.path:
                    _sys.path.insert(0, _repo)
                from synth.config import GeneratorConfig, Preset
                from synth.generate import generate as _generate

                cfg = GeneratorConfig.for_preset(
                    Preset.WELL_IDENTIFIED,
                    seed=DEMO_SEED,
                    n_customers=DEMO_CUSTOMERS,
                    n_items=DEMO_ITEMS,
                    # Same substitution structure as the committed sample, so
                    # the cross-price module is exercised on the demo site and
                    # the world matches the scanned causal-reaching candidate.
                    n_substitution_groups=4,
                )
                world = Path(td) / "world"
                _generate(cfg, world)
                data = world / "erp"
                mapping = Path(__file__).resolve().parents[2] / "config/mappings/synth_erp.yaml"
                label = (
                    f"Synthetic demo data (generated world, seed {DEMO_SEED}, "
                    f"{DEMO_CUSTOMERS} customers x {DEMO_ITEMS} items, planted ground truth)"
                )
            if data is None or mapping is None:
                typer.echo("ERROR: --data and --mapping are required without --demo", err=True)
                raise typer.Exit(code=2)
            result = run_full(data, mapping, rules, Path(td) / "engine_out")
            bundle = build_bundle(
                result,
                _load_rules(rules),
                Path(__file__).resolve().parents[2],
                data_label=label,
            )
    except MappingError as exc:
        _fail("mapping", exc)
    except MoneyParseError as exc:
        _fail("money parsing", exc)
    except ValidationError as exc:
        _fail("configuration", exc)
    except ReconciliationError as exc:
        _fail("reconciliation", exc)
    bundle_path, schema_path = write_bundle(bundle, out)
    size_kb = bundle_path.stat().st_size / 1024
    typer.echo(f"Bundle: {bundle_path} ({size_kb:,.0f} KB)")
    typer.echo(f"Schema: {schema_path}")
    typer.echo(
        f"Guidance pairs: {len(bundle.guidance)}; scenario grids: {len(bundle.scenario_grids)}"
    )


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
