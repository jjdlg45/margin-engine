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
from margin_engine.fiscal import FiscalCalendarError
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
    validate: bool = typer.Option(
        False,
        "--validate",
        help="Also run the real-data validation suite (temporal backtest, "
        "placebos, rolling-window stability); off by default because it "
        "dominates runtime (Phase 35)",
    ),
    cache_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--cache-dir",
        help="Directory for the fingerprint-keyed parse cache: re-running on "
        "unchanged input skips re-parsing the export (Phase 35)",
    ),
    spill: bool = typer.Option(
        False,
        "--spill",
        help="Spill the ledger to Parquet for the analytic stages (Phase 83): "
        "lower peak memory on large worlds, byte-identical outputs by test. "
        "Also available as scale.spill_ledger in the rules file",
    ),
) -> None:
    """Run the full pipeline: ingest -> waterfall -> analytics -> report."""
    from margin_engine.report.full_run import run_full

    try:
        result = run_full(
            data,
            mapping,
            rules,
            out,
            validate=validate,
            cache_dir=cache_dir,
            spill=spill or None,
        )
    except MappingError as exc:
        _fail("mapping", exc)
    except MoneyParseError as exc:
        _fail("money parsing", exc)
    except ValidationError as exc:
        _fail("configuration", exc)
    except ReconciliationError as exc:
        _fail("reconciliation", exc)
    except FiscalCalendarError as exc:
        _fail("fiscal calendar", exc)
    typer.echo(f"Ledger lines: {result.pipeline.ledger.height}")
    typer.echo(f"Reconciliation: {'PASS' if result.pipeline.reconciliation.all_pass else 'FAIL'}")
    for stage, secs in result.timings.items():
        typer.echo(f"  {stage}: {secs:.1f}s")
    typer.echo(f"Report: {out / 'report.html'}")
    typer.echo(f"Manifest: {out / 'run_manifest.json'}")


@app.command()
def validate(
    data: Path = typer.Option(..., "--data", help="Directory with the source export"),  # noqa: B008
    mapping: Path = typer.Option(..., "--mapping", help="Mapping YAML for the source"),  # noqa: B008
    rules: Path = typer.Option(  # noqa: B008
        Path("config/rules.yaml"), "--rules", help="Policy rules YAML"
    ),
    out: Path = typer.Option(Path("output"), "--out", help="Output directory"),  # noqa: B008
) -> None:
    """Run the real-data validation suite (temporal backtest, placebo tests,
    rolling-window stability) and write its exports. Equivalent to
    `run --validate`; exists so routine runs stay fast (Phase 35)."""
    from margin_engine.report.full_run import run_full

    try:
        result = run_full(data, mapping, rules, out, validate=True)
    except MappingError as exc:
        _fail("mapping", exc)
    except MoneyParseError as exc:
        _fail("money parsing", exc)
    except ValidationError as exc:
        _fail("configuration", exc)
    except ReconciliationError as exc:
        _fail("reconciliation", exc)
    except FiscalCalendarError as exc:
        _fail("fiscal calendar", exc)
    typer.echo("Real-data validation (no planted truth; informational, never gates a status):")
    for name in (
        "real_validation_backtest",
        "real_validation_placebo",
        "real_validation_stability_elasticity",
        "real_validation_stability_segments",
    ):
        path = out / f"{name}.csv"
        typer.echo(f"  {name}: {path if path.exists() else 'not produced'}")
    secs = result.timings.get("real_data_validation")
    if secs is not None:
        typer.echo(f"  validation runtime: {secs:.1f}s")


@app.command()
def export_prices(
    actions_db: Path = typer.Option(  # noqa: B008
        ..., "--actions-db", help="Action store (DuckDB) holding reviewed actions"
    ),
    out: Path = typer.Option(Path("prices.csv"), "--out", help="Price file to write"),  # noqa: B008
    rules: Path = typer.Option(  # noqa: B008
        Path("config/rules.yaml"),
        "--rules",
        help="Policy rules YAML (actions.price_export template)",
    ),
    who: str = typer.Option("cli", "--who", help="Recorded in the exported transition events"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the diff summary without writing or changing state"
    ),
) -> None:
    """Export approved price actions as an ERP-ready price file (Phase 40).
    The diff summary prints BEFORE the file is written; re-exporting is
    idempotent (identical file, no repeated transitions)."""
    from margin_engine.actions.export import export_price_file
    from margin_engine.actions.store import ActionStore

    try:
        loaded = load_rules(rules)
    except (ValidationError, ValueError, TypeError) as exc:
        _fail("configuration", exc)
    store = ActionStore(actions_db)
    preview = export_price_file(store, loaded, None, who=who, dry_run=True)
    s = preview.summary
    typer.echo(f"Price changes to export: {s['n_changes']}")
    typer.echo("Expected impact by basis (never summed across levels):")
    for basis, cents in dict(s["expected_impact_by_basis_cents"]).items():  # type: ignore[call-overload]
        typer.echo(f"  {basis}: ${int(cents) / 100:,.2f}")
    for ch in list(s["largest_changes"]):  # type: ignore[call-overload]
        typer.echo(
            f"  largest: {ch['customer_id']} / {ch['item_id']} "
            f"{ch['change_pct']:+.2f}% (action {ch['action_id']})"
        )
    if dry_run:
        typer.echo("Dry run: nothing written, no state changed.")
        return
    result = export_price_file(store, loaded, out, who=who)
    typer.echo(f"Wrote {out} and {out.name}.summary.json beside it")
    typer.echo(f"Actions moved to exported: {result.n_transitioned}")
    _rec_raw = result.summary.get("n_recovered_exports") or 0
    n_rec = int(_rec_raw) if isinstance(_rec_raw, int | float | str) else 0
    if n_rec:
        typer.echo(
            f"Recovered {n_rec} action(s) from an export that was interrupted "
            f"before its statuses were written"
        )
    held = list(result.summary.get("held_out_stale") or [])  # type: ignore[call-overload]
    for h in held:
        typer.echo(f"  held out (stale): {h['action_id']}: {h['reason']}")


@app.command()
def track(
    actions_db: Path = typer.Option(  # noqa: B008
        ..., "--actions-db", help="Action store (DuckDB) with implemented actions"
    ),
    data: Path = typer.Option(..., "--data", help="Directory with the source export"),  # noqa: B008
    mapping: Path = typer.Option(..., "--mapping", help="Mapping YAML for the source"),  # noqa: B008
    rules: Path = typer.Option(  # noqa: B008
        Path("config/rules.yaml"), "--rules", help="Policy rules YAML"
    ),
    out: Path | None = typer.Option(  # noqa: B008
        None, "--out", help="Also write action_outcomes.csv/.parquet here"
    ),
) -> None:
    """Measure implemented actions against the ledger (Phase 41): price
    realization, volume vs prediction with an untreated same-segment comparison
    group (a control-adjusted volume change), margin before vs after. Newly measured
    actions move implemented -> measured; gates fail closed with reasons."""
    import tempfile

    from margin_engine.actions.store import ActionStore
    from margin_engine.actions.tracking import measure_and_record
    from margin_engine.report.full_run import run_full

    try:
        loaded = load_rules(rules)
        with tempfile.TemporaryDirectory() as td:
            result_full = run_full(data, mapping, rules, Path(td) / "out", validate=False)
    except MappingError as exc:
        _fail("mapping", exc)
    except MoneyParseError as exc:
        _fail("money parsing", exc)
    except ValidationError as exc:
        _fail("configuration", exc)
    except ReconciliationError as exc:
        _fail("reconciliation", exc)
    except FiscalCalendarError as exc:
        _fail("fiscal calendar", exc)
    store = ActionStore(actions_db)
    pair_segments = result_full.guidance.select("customer_id", "item_id", "segment_leaf")
    result = measure_and_record(
        store, result_full.pipeline.ledger, loaded, pair_segments=pair_segments
    )
    for note in result.notes:
        typer.echo(note)
    for row in result.calibration.to_dicts():
        if row["status"] == "not_enough_implemented_actions":
            typer.echo(
                f"Calibration: not enough implemented actions yet ({row['n_measured']} measured)"
            )
        else:
            typer.echo(
                f"Calibration [{row['bucket']}]: n={row['n_measured']}, "
                f"median |volume error| = {row['median_abs_volume_error_log_points']} "
                f"log points, median realization = {row['median_realization_pct']}"
            )
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        result.outcomes.write_parquet(out / "action_outcomes.parquet")
        result.outcomes.write_csv(out / "action_outcomes.csv")
        result.calibration.write_csv(out / "action_calibration.csv")
        typer.echo(f"Outcomes written to {out}")


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
    except FiscalCalendarError as exc:
        _fail("fiscal calendar", exc)
    bundle_path, schema_path = write_bundle(bundle, out)
    # Action store seeds for API mode (Phase 42): the live BFF merges these at
    # startup (MARGIN_API_ACTIONS_SEED); the static site never fetches them.
    if result.actions is not None and result.action_events is not None:
        result.actions.write_parquet(out / "actions.parquet")
        result.action_events.write_parquet(out / "action_events.parquet")
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


@app.command()
def profile(
    file: Path = typer.Argument(..., help="CSV or .xlsx export to profile"),  # noqa: B008
    sheet: str | None = typer.Option(None, "--sheet", help=".xlsx sheet name"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
) -> None:
    """Profile an export: formats, per-column stats, canonical-field candidates
    with confidence and reason, suspected transaction-type codes (Run 4, 27.1).
    Every guess needs human confirmation; nothing here becomes a decision."""
    import dataclasses
    import json as _json

    from margin_engine.ingest.mapping import MappingError
    from margin_engine.ingest.profiler import profile_file, render_profile

    try:
        result = profile_file(file, sheet)
    except (MappingError, OSError) as exc:
        _fail("profiling", exc)
    if as_json:
        typer.echo(_json.dumps(dataclasses.asdict(result), indent=1))
    else:
        typer.echo(render_profile(result))


@app.command()
def draft_mapping(
    file: Path = typer.Argument(..., help="CSV or .xlsx export to draft a mapping for"),  # noqa: B008
    out: Path | None = typer.Option(  # noqa: B008
        None, "--out", help="Where to write the draft YAML (default: <file>.mapping.yaml)"
    ),
    sheet: str | None = typer.Option(None, "--sheet", help=".xlsx sheet name"),
) -> None:
    """Write a draft mapping YAML from the profile. Every guessed value is
    marked # CONFIRM and listed under unconfirmed_fields; the engine refuses
    to run the mapping until a person empties that list (Run 4, 27.1)."""
    from margin_engine.ingest.mapping import MappingError
    from margin_engine.ingest.profiler import draft_mapping as _draft
    from margin_engine.ingest.profiler import profile_file

    try:
        result = profile_file(file, sheet)
    except (MappingError, OSError) as exc:
        _fail("profiling", exc)
    target = out if out is not None else file.with_suffix(file.suffix + ".mapping.yaml")
    target.write_text(_draft(result), encoding="utf-8", newline="\n")
    typer.echo(f"Draft mapping written to {target}")
    typer.echo(
        "It will NOT run as-is: review every '# CONFIRM' line and delete its "
        "entry from unconfirmed_fields once confirmed."
    )


@app.command()
def anonymize(
    data: Path = typer.Option(..., "--data", help="Directory with the source export"),  # noqa: B008
    mapping: Path = typer.Option(..., "--mapping", help="Mapping YAML for the source"),  # noqa: B008
    out: Path = typer.Option(..., "--out", help="Directory for the anonymized copy"),  # noqa: B008
    salt_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--salt-file",
        help="File holding the secret salt (>=16 bytes); else MARGIN_ENGINE_ANON_SALT",
    ),
    scale_money: bool = typer.Option(
        False,
        "--scale-money",
        help="Also scale every mapped money column by a secret salt-derived factor",
    ),
) -> None:
    """Anonymize an export (Run 4, 27.5): salted keyed hashes for customer,
    item and supplier IDs and names; optional secret money scaling. The salt
    never appears in any output or manifest."""
    from margin_engine.anonymize import anonymize_export, load_salt
    from margin_engine.ingest.mapping import MappingError, load_mapping

    try:
        m = load_mapping(mapping)
        salt = load_salt(salt_file)
        counts = anonymize_export(data, m, out, salt, scale_money=scale_money)
    except MappingError as exc:
        _fail("anonymization", exc)
    total = sum(counts.values())
    typer.echo(f"Anonymized {len(counts)} file(s), {total:,} rows, into {out}")
    typer.echo("IDs and names are salted keyed hashes; the salt is not recorded anywhere.")
    if scale_money:
        typer.echo("Money columns were scaled by a secret factor (internally consistent).")


if __name__ == "__main__":
    app()
