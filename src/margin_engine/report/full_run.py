"""Full Run-1 pipeline orchestration (Phase 8).

ingest -> classify -> waterfall -> reconcile -> dispersion -> segmentation ->
guidance -> panel -> sensitivity -> opportunity -> outputs (parquet/csv),
HTML report, run manifest, DuckDB store. Stage timings recorded.
"""

from __future__ import annotations

import re
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from margin_engine.actions.generate import generate_actions
from margin_engine.compliance import run_robinson_patman
from margin_engine.config import load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.extended.cost_to_serve import run_cost_to_serve
from margin_engine.extended.hierarchy import apply_analysis_level
from margin_engine.fiscal import FiscalCalendar, build_calendar
from margin_engine.guidance.bands import PROVISIONAL_LABEL, build_guidance
from margin_engine.labels import describe, plain_rule
from margin_engine.labels import label as label_of
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline, store_duckdb
from margin_engine.manifest.run_manifest import (
    build_manifest,
    cleaning_decisions_from,
    write_manifest,
)
from margin_engine.memlog import rss_log
from margin_engine.money import cents_col_to_dollars
from margin_engine.opportunity.sizing import observed_leakage, price_opportunities
from margin_engine.panel.build import build_panel
from margin_engine.report.render import render_report
from margin_engine.report.tieout import TieoutResult, run_tieout
from margin_engine.segment.trees import (
    TreeResult,
    fit_commercial_segments,
    fit_margin_driver_tree,
    prepare_segmentation_frame,
)
from margin_engine.sensitivity.estimate import run_sensitivity


@dataclass
class FullRunResult:
    pipeline: PipelineResult
    flagged: pl.DataFrame
    segments: TreeResult
    guidance: pl.DataFrame
    sensitivity: pl.DataFrame
    opportunities: pl.DataFrame
    leakage: pl.DataFrame
    timings: dict[str, float] = field(default_factory=dict)
    out_dir: Path | None = None
    # Everything below exists so downstream consumers (the web-bundle export)
    # can reuse this run's frames without recomputation. All additive.
    panel: pl.DataFrame | None = None
    margin_tree: TreeResult | None = None
    peer_stats: pl.DataFrame | None = None
    order_economics: pl.DataFrame | None = None
    customer_economics: pl.DataFrame | None = None
    service_flags: pl.DataFrame | None = None
    win_curves: pl.DataFrame | None = None
    cross_estimates: pl.DataFrame | None = None
    inventory_cogs: pl.DataFrame | None = None
    inventory_identity: pl.DataFrame | None = None
    tieout: TieoutResult | None = None
    unallocated_settlements: pl.DataFrame | None = None
    library_warnings: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    fiscal: FiscalCalendar | None = None  # the calendar in force (Phase 36)
    actions: pl.DataFrame | None = None  # generated action proposals (Phase 38)
    action_events: pl.DataFrame | None = None  # their birth event log (Phase 38)


def _fmt_dollars(cents: int | float | None) -> str:
    if cents is None:
        return "-"
    return f"{cents / 100:,.2f}"


# Plain-English explanation of every visible residual: what it is, why the
# dollars are not forced onto lines, and where the amount is listed.
_RESIDUAL_EXPLANATIONS: dict[str, dict[str, str]] = {
    "settlement_rebates_unallocated_cents": {
        "what": "Rebate settlement dollars whose customer-period group has no invoice lines.",
        "why": "Spreading them over unrelated SKUs would fabricate line-level margins.",
        "where": "Per customer, period and program in unallocated_settlements.csv.",
    },
    "tiered_settlements_unallocated_cents": {
        "what": "Tiered-program settlements with no matching lines in the measurement period.",
        "why": "No eligible lines exist to carry them; forcing them would distort other items.",
        "where": "Per customer, period and program in unallocated_settlements.csv.",
    },
    "tiered_true_up_cents": {
        "what": "Timing difference between rebates accrued during the period (estimated) "
        "and the amount settled at true-up (actual).",
        "why": "It is a period-boundary effect, not a property of any line; restating "
        "historical lines would break their audit trail.",
        "where": "reconciliation.csv; the waterfall labels which basis each line uses.",
    },
    "return_freight_unallocated_cents": {
        "what": "Return freight expense from the log that matched no return lines.",
        "why": "Charging it to unrelated returns would misstate their recovery economics.",
        "where": "reconciliation.csv, as a visible pool-level cost.",
    },
    "billed_charges_unallocated_cents": {
        "what": "Order-level billed charges whose order has no sale lines to carry them.",
        "why": "There is no defensible allocation basis without lines.",
        "where": "reconciliation.csv.",
    },
    "price_credits_unattributed_cents": {
        "what": "Price-correction credit memos whose original invoice could not be identified.",
        "why": "Attributing them to guessed lines would silently rewrite realized prices.",
        "where": "reconciliation.csv; the credits remain in the financial ledger.",
    },
    "freight_expense_unallocated_cents": {
        "what": "Outbound freight expense with no allocation basis (no weight, cube or "
        "lines in its group).",
        "why": "Allocation without a driver would be arbitrary; the policy keeps a "
        "visible residual instead.",
        "where": "reconciliation.csv, as a cost-to-serve pool.",
    },
    "claims_unallocated_cents": {
        "what": "Non-price claim credits with no matching lines.",
        "why": "They belong to a cost category, not to specific unrelated SKUs.",
        "where": "reconciliation.csv.",
    },
    "kit_realloc_net_cents": {
        "what": "Net rounding of kit revenue reallocation across components.",
        "why": "A nonzero value flags a kit allocation problem to investigate, "
        "never silently absorbed into component lines.",
        "where": "reconciliation.csv.",
    },
}


def _aggregate_warnings(caught: list[warnings.WarningMessage]) -> list[dict[str, object]]:
    """Group captured library warnings by category and digit-normalized message
    so '17 singletons dropped' and '11 singletons dropped' collapse to one row."""
    groups: dict[tuple[str, str], int] = {}
    for w in caught:
        key = (w.category.__name__, re.sub(r"\d+", "N", str(w.message)))
        groups[key] = groups.get(key, 0) + 1
    return [
        {"category": cat, "message": msg, "n_occurrences": n}
        for (cat, msg), n in sorted(groups.items())
    ]


# Natural sort key per export, applied just before writing so every file is
# byte-identical across runs on the same input. First matching column set in
# the priority list wins; frames with their own meaningful order (peer stats,
# win curves, reconciliation checks) are already deterministic upstream.
_EXPORT_KEYS: dict[str, list[str]] = {
    "financial_ledger": ["source_line_key"],
    "sales_observations_flagged": ["source_line_key"],
    "segment_assignments": ["source_line_key"],
    "segment_rules": ["leaf_id"],
    "margin_driver_rules": ["leaf_id"],
    "guidance": ["customer_id", "item_id"],
    "compliance_robinson_patman": ["item_id", "customer_id_a", "customer_id_b"],
    "actions": ["action_type", "action_id"],
    "action_events": ["action_id", "seq"],
    "sensitivity": ["segment"],
    "opportunities": ["customer_id", "item_id"],
    "order_economics": ["order_id"],
    "customer_economics": ["customer_id"],
    "service_policy_flags": ["customer_id"],
    "inventory_cogs": ["source_line_key"],
    "inventory_identity": ["item_id"],
    "win_curves": ["segment"],
    "cross_price_estimates": ["group_id", "item_a", "item_b"],
    "unallocated_settlements": ["customer_id", "period", "rebate_program_id"],
}


def _natural_sort(name: str, frame: pl.DataFrame) -> pl.DataFrame:
    keys = [k for k in _EXPORT_KEYS.get(name, []) if k in frame.columns]
    return frame.sort(keys) if keys else frame


# Exports carrying iterative-solver estimates (PPML/IRLS, logistic Newton,
# fixed-effects OLS): BLAS threading and solver iteration order can move the
# last few ulps between otherwise identical runs, so these frames are rounded
# to 10 significant digits at export time only. In-memory results are
# untouched; 10 digits is far beyond any decision-relevant precision.
_SOLVER_EXPORTS = frozenset({"sensitivity", "win_curves", "cross_price_estimates"})


def _round_solver_outputs(name: str, frame: pl.DataFrame) -> pl.DataFrame:
    if name not in _SOLVER_EXPORTS:
        return frame
    cols = [c for c, dt in frame.schema.items() if dt in (pl.Float64, pl.Float32)]
    if not cols:
        return frame
    return frame.with_columns(pl.col(c).round_sig_figs(10).alias(c) for c in cols)


def run_full(
    data_dir: Path,
    mapping_path: Path,
    rules_path: Path,
    out_dir: Path,
    *,
    validate: bool | None = None,
    lines: pl.DataFrame | None = None,
    cache_dir: Path | None = None,
) -> FullRunResult:
    """Run the pipeline. `validate=None` follows the config switch
    (sensitivity.real_data_validation); an explicit bool overrides it — the CLI
    `run` command passes False unless --validate is given. `lines` and
    `cache_dir` pass through to run_pipeline (Phase 35)."""
    rules = load_rules(rules_path)
    timings: dict[str, float] = {}
    out_dir.mkdir(parents=True, exist_ok=True)

    # Capture library warnings (e.g. pyfixest singleton fixed effects) for the
    # manifest and report instead of printing them to the console. Entered and
    # exited manually so the whole pipeline body needs no re-indentation.
    _w_catcher = warnings.catch_warnings(record=True)
    _caught = _w_catcher.__enter__()
    warnings.simplefilter("always")

    t = time.perf_counter()
    res = run_pipeline(data_dir, mapping_path, rules, lines=lines, cache_dir=cache_dir)
    timings["ingest_classify_waterfall_reconcile"] = time.perf_counter() - t
    rss_log("pipeline_done")

    # Fiscal calendar (Phase 36): one calendar drives every ANALYTIC period —
    # panel periods, peer-group quarters, the guidance observation unit,
    # baseline windows, period counts, the books tie-out. Data-declared
    # periods (settlements, landed cost, FX months) stay data-defined.
    fiscal = build_calendar(rules.fiscal, res.reference.get("fiscal_periods"))
    res.notes.extend(fiscal.notes)
    if not fiscal.is_calendar_months:
        res.notes.append(
            f"fiscal calendar '{fiscal.kind}' in force: analytic periods, "
            "quarters and baseline windows follow it; monthly instrument and "
            "market-index tables join by the calendar month containing each "
            "fiscal period's start (approximation, stated here)"
        )

    t = time.perf_counter()
    cts = run_cost_to_serve(res.ledger, rules, fiscal)
    res.ledger = cts.lines
    res.notes.extend(cts.notes)
    timings["cost_to_serve"] = time.perf_counter() - t
    rss_log("cost_to_serve")

    t = time.perf_counter()
    analysis_obs, h_notes = apply_analysis_level(
        res.observations, res.reference["customers"], rules
    )
    res.notes.extend(h_notes)
    flagged, peer_stats, _abc = run_dispersion(analysis_obs, res.reference["items"], rules, fiscal)
    del analysis_obs  # superseded by `flagged`; keep one fewer wide copy alive (Phase 35)
    timings["dispersion_outliers_abc"] = time.perf_counter() - t
    rss_log("dispersion")

    t = time.perf_counter()
    df = prepare_segmentation_frame(flagged, res.reference["customers"], res.reference["items"])
    seg = fit_commercial_segments(df, rules, seed=0)
    df = df.join(seg.leaf_assignments, on="source_line_key", how="left")
    margin_tree = fit_margin_driver_tree(df, rules, seed=0)
    timings["segmentation"] = time.perf_counter() - t
    rss_log("segmentation")

    t = time.perf_counter()
    # Per-unit cost-to-serve K (estimated, from config activity rates) for the
    # optional contribution floor (24.3): pair totals over covered sale lines.
    cts_per_unit = (
        res.ledger.filter(
            (pl.col("transaction_type") == "sale")
            & (pl.col("quantity_norm") > 0)
            & pl.col("activity_cts_cents").is_not_null()
        )
        .group_by("customer_id", "item_id")
        .agg(
            pl.col("activity_cts_cents").sum().alias("_cts"),
            pl.col("quantity_norm").sum().alias("_qty"),
        )
        .filter(pl.col("_qty") > 0)
        .with_columns(
            (pl.col("_cts") / pl.col("_qty")).round(0).cast(pl.Int64).alias("cts_per_unit_cents")
        )
        .select("customer_id", "item_id", "cts_per_unit_cents")
        if "activity_cts_cents" in res.ledger.columns
        else None
    )
    guidance_res = build_guidance(df, rules, res.reference["rebate_programs"], cts_per_unit, fiscal)
    # Pricing compliance (Phase 37): Robinson-Patman review flag on competitor
    # pairs, with cost-to-serve differences as supporting documentation.
    compliance_res = run_robinson_patman(
        guidance_res.guidance, res.reference["customers"], cts_per_unit, rules
    )
    res.notes.extend(compliance_res.notes)
    guidance_res.guidance = guidance_res.guidance.join(
        compliance_res.flags, on=["customer_id", "item_id"], how="left", maintain_order="left"
    ).with_columns(pl.col("rp_review_flag").fill_null(value=False))
    timings["guidance"] = time.perf_counter() - t
    rss_log("guidance")

    t = time.perf_counter()
    # Public-data-only rule (Phase 37): the market/competitor price index is
    # accepted as an elasticity control only when the mapping declares its
    # source PUBLIC. Algorithmic use of competitors' non-public prices raises
    # antitrust concerns; the engine refuses the control rather than assume.
    market_idx = res.reference.get("market_price_index")
    if market_idx is not None and rules.compliance.market_index_public_source_required:
        mpi_spec = res.mapping.reference_tables.get("market_price_index")
        if mpi_spec is None or mpi_spec.public_source is not True:
            market_idx = None
            res.notes.append(
                "market price index NOT used as an elasticity control: the "
                "mapping does not declare public_source: true for it. Only "
                "public sources are accepted (sharing competitors' non-public "
                "prices through a pricing algorithm raises antitrust concerns; "
                "see docs/methodology.md, Pricing-law considerations)"
            )
    # Instrument builder (27.6): the supplier cost instrument can come from a
    # mapped index table, be BUILT from receipt history, or map an external
    # public series - config decides, the engine never guesses.
    supplier_idx = res.reference["supplier_cost_index"]
    src_mode = rules.sensitivity.instrument_source
    if src_mode == "receipts":
        from margin_engine.sensitivity.instruments import build_supplier_cost_index

        receipts_ref = res.reference.get("inventory_receipts")
        items_ref = res.reference.get("items")
        if receipts_ref is None or items_ref is None:
            res.notes.append(
                "instrument_source=receipts but no inventory_receipts/items table: "
                "supplier cost instrument unavailable (statuses cap accordingly)"
            )
            supplier_idx = None
        else:
            supplier_idx, inotes = build_supplier_cost_index(receipts_ref, items_ref)
            res.notes.extend(inotes)
    elif src_mode == "external":
        from margin_engine.sensitivity.instruments import external_cost_index

        series_ref = res.reference.get("external_cost_series")
        items_ref = res.reference.get("items")
        if series_ref is None or items_ref is None:
            res.notes.append(
                "instrument_source=external but no external_cost_series/items table: "
                "supplier cost instrument unavailable (statuses cap accordingly)"
            )
            supplier_idx = None
        else:
            supplier_idx, inotes = external_cost_index(
                series_ref, rules.sensitivity.external_index_map, items_ref
            )
            res.notes.extend(inotes)
    panel = build_panel(
        df,
        res.reference["items"],
        supplier_idx,
        res.reference["tariff_schedule"],
        rules,
        market_price_index=market_idx,
        fiscal=fiscal,
    )
    timings["panel"] = time.perf_counter() - t
    rss_log("panel")

    t = time.perf_counter()
    sens = run_sensitivity(panel, rules)
    timings["sensitivity"] = time.perf_counter() - t
    rss_log("sensitivity")

    t = time.perf_counter()
    leak = observed_leakage(res.ledger, rules, fiscal)
    opps = price_opportunities(guidance_res.guidance, panel, sens, rules)
    timings["opportunity"] = time.perf_counter() - t
    rss_log("opportunity")

    # Validation without planted truth (27.4): temporal backtest, placebos,
    # rolling-window stability. Informational; never gates a status.
    real_val = None
    do_validate = rules.sensitivity.real_data_validation if validate is None else validate
    if do_validate:
        from margin_engine.sensitivity.real_validation import run_real_validation

        t = time.perf_counter()
        real_val = run_real_validation(panel, df, rules, seed=0, fiscal=fiscal)
        res.notes.extend(real_val.notes)
        timings["real_data_validation"] = time.perf_counter() - t
        rss_log("real_data_validation")
    else:
        res.notes.append(
            "real-data validation OFF: disabled in config"
            if validate is None
            else "real-data validation OFF: run with --validate or use the "
            "validate command to include it"
        )

    # --- optional Run-2 modules: each runs when its input exists, otherwise
    # switches off with a stated reason -------------------------------------
    t = time.perf_counter()
    inventory_cogs: pl.DataFrame | None = None
    inventory_identity: pl.DataFrame | None = None
    if res.reference["inventory_receipts"] is not None:
        from margin_engine.extended.inventory import cost_sales

        res.pipeline_cost_basis if hasattr(res, "pipeline_cost_basis") else None
        method = {"standard": "standard", "average": "average", "fifo": "fifo", "lifo": "lifo"}.get(
            res.mapping.lines.unit_cost.cost_basis.value if res.mapping.lines.unit_cost else "",
            "average",
        )
        inv_sales = res.observations.filter(pl.col("quantity_norm") > 0).select(
            "source_line_key", "item_id", "invoice_date", "quantity"
        )
        inv = cost_sales(res.reference["inventory_receipts"], inv_sales, method)
        inventory_cogs = inv.per_sale
        inventory_identity = inv.identity
        res.notes.append(f"inventory costing ran with method '{method}' (declared cost basis)")
    else:
        res.notes.append("inventory costing OFF: no inventory_receipts table in the mapping/data")

    win_curves: pl.DataFrame | None = None
    if res.reference["quotes"] is not None and res.reference["price_list"] is not None:
        from margin_engine.extended.quotes import fit_win_curves, quote_price_index

        seg_of_pair = (
            df.filter(pl.col("segment_leaf").is_not_null())
            .select("customer_id", "item_id", "segment_leaf")
            .unique(subset=["customer_id", "item_id"])
        )
        qi = quote_price_index(res.reference["quotes"], res.reference["price_list"])
        win_curves = fit_win_curves(
            qi,
            seg_of_pair,
            min_quotes=rules.sensitivity.min_quotes_per_segment,
            max_rel_se=rules.sensitivity.win_curve_max_rel_se,
        )
        res.notes.append("win/loss modeling ran on the quotes table")
    else:
        res.notes.append("win/loss modeling OFF: no quotes table (or no price list) available")

    # Books tie-out (27.3): runs only when a gl_totals table is mapped.
    tieout_res = None
    gl_totals = res.reference.get("gl_totals")
    if gl_totals is not None:
        tieout_res = run_tieout(res.ledger, gl_totals, rules, fiscal)
        res.notes.extend(tieout_res.notes)
        res.notes.append(
            f"books tie-out ran: {tieout_res.n_failing} month-measure(s) outside tolerance"
            if tieout_res.n_failing
            else "books tie-out ran: every month within tolerance"
        )
    else:
        res.notes.append("books tie-out OFF: no gl_totals table in the mapping/data")

    cross_estimates: pl.DataFrame | None = None
    if res.reference["substitution_groups"] is not None:
        from margin_engine.extended.substitution import estimate_cross_effects

        cross_estimates = estimate_cross_effects(panel, res.reference["substitution_groups"], rules)
        res.notes.append("cross-price estimation ran within declared substitution groups")
    else:
        res.notes.append("cross-price estimation OFF: no substitution_groups table available")
    timings["optional_modules"] = time.perf_counter() - t
    rss_log("optional_modules")
    # The segmentation frame is consumed; only its leaf assignments, guidance,
    # the panel and `flagged` live on (Phase 35: one fewer wide line-level frame).
    del df

    # ---- actions (Phase 38): findings become proposed actions ---------------
    t = time.perf_counter()
    actions_res = generate_actions(
        ledger=res.ledger,
        guidance=guidance_res.guidance,
        opportunities=opps,
        leakage=leak,
        service_flags=cts.service_flags,
        sensitivity=sens,
        panel=panel,
        rules=rules,
    )
    res.notes.extend(actions_res.notes)
    timings["actions"] = time.perf_counter() - t

    # ---- outputs -----------------------------------------------------------
    t = time.perf_counter()
    exports: dict[str, pl.DataFrame] = {
        "financial_ledger": res.ledger,
        "sales_observations_flagged": flagged,
        "peer_group_stats": peer_stats,
        "segment_assignments": seg.leaf_assignments,
        "segment_rules": pl.DataFrame(seg.rules),
        "margin_driver_rules": pl.DataFrame(margin_tree.rules),
        "guidance": guidance_res.guidance,
        "compliance_robinson_patman": compliance_res.detail,
        "actions": actions_res.actions,
        "action_events": actions_res.events,
        "sensitivity": sens,
        "opportunities": opps,
        "leakage": leak,
        "order_economics": cts.order_economics,
        **({"inventory_cogs": inventory_cogs} if inventory_cogs is not None else {}),
        **({"inventory_identity": inventory_identity} if inventory_identity is not None else {}),
        **({"win_curves": win_curves} if win_curves is not None else {}),
        **({"cross_price_estimates": cross_estimates} if cross_estimates is not None else {}),
        "customer_economics": cts.customer_economics,
        "service_policy_flags": cts.service_flags,
        "reconciliation": res.reconciliation.to_frame(),
        **({"books_tieout": tieout_res.table} if tieout_res is not None else {}),
        **(
            {
                "real_validation_backtest": real_val.backtest,
                "real_validation_placebo": real_val.placebo,
                "real_validation_stability_elasticity": real_val.stability_elasticity,
                "real_validation_stability_segments": real_val.stability_segments,
            }
            if real_val is not None
            else {}
        ),
    }
    unalloc_frame = (
        res.unallocated_settlements
        if res.unallocated_settlements is not None
        else pl.DataFrame(
            schema={
                "customer_id": pl.String,
                "period": pl.String,
                "rebate_program_id": pl.String,
                "amount_cents": pl.Int64,
            }
        )
    )
    # Kept in its export (sorted) form: the report and the web bundle iterate
    # its rows in file order.
    unalloc_sorted = _natural_sort("unallocated_settlements", unalloc_frame)
    exports["unallocated_settlements"] = unalloc_sorted
    # One export at a time (Phase 35): the write-ready copy — categorical code
    # columns back to plain String (every file keeps its pre-refactor schema,
    # bytes included), natural sort, solver rounding — exists only while that
    # file is being written, never as a second full set of frames.
    formats = list(rules.reporting.output_formats)
    prewritten: dict[str, Path] = {}
    for name, frame in exports.items():
        out = _round_solver_outputs(
            name, _natural_sort(name, frame.with_columns(pl.col(pl.Categorical).cast(pl.String)))
        )
        for fmt in formats:
            if fmt == "parquet":
                out.write_parquet(out_dir / f"{name}.parquet")
                prewritten[name] = out_dir / f"{name}.parquet"
            elif fmt == "csv":
                # _usd columns are derived only here, at the CSV boundary; the
                # engine carries integer cents throughout. One lazy pass
                # streams the converted frame to disk instead of holding a
                # converted copy in memory.
                cents_cols = [
                    col
                    for col, dtype in out.schema.items()
                    if col.endswith("_cents") and dtype == pl.Int64
                ]
                lf = out.lazy()
                if cents_cols:
                    lf = lf.with_columns(
                        cents_col_to_dollars(c).alias(c.replace("_cents", "_usd"))
                        for c in cents_cols
                    ).drop(cents_cols)
                try:
                    lf.sink_csv(out_dir / f"{name}.csv")
                except pl.exceptions.PolarsError:
                    lf.with_columns(pl.col(pl.Date).cast(pl.String)).sink_csv(
                        out_dir / f"{name}.csv"
                    )
        del out
    rss_log("export_files_written")
    store_duckdb(res, out_dir / "engine.duckdb", prewritten=prewritten)
    rss_log("duckdb_stored")
    timings["exports"] = time.perf_counter() - t

    # ---- manifest -----------------------------------------------------------
    _w_catcher.__exit__(None, None, None)
    lib_warnings = _aggregate_warnings(list(_caught or []))
    manifest = build_manifest(
        data_dir=data_dir,
        mapping_path=mapping_path,
        rules_path=rules_path,
        cleaning_decisions=cleaning_decisions_from(
            res.ledger, res.removed_duplicates, res.blank_rows_dropped
        ),
        parameters={
            "n_segments": seg.leaf_assignments["segment_leaf"].n_unique(),
            "bootstrap_median_ari": seg.bootstrap_median_ari,
            "notes": res.notes + seg.notes + guidance_res.notes,
            "library_warnings": lib_warnings,
        },
        stage_timings_s=timings,
    )
    write_manifest(manifest, out_dir)

    # ---- report --------------------------------------------------------------
    sale = res.ledger.filter(pl.col("transaction_type") == "sale")
    net_rev = int(sale["net_amount_cents"].sum())
    missing_cost_rev = int(sale.filter(pl.col("flag_missing_cost"))["net_amount_cents"].sum())
    gross_list = int(sale["gross_list_value_cents"].fill_null(0).sum())
    list_coverage = sale.filter(pl.col("gross_list_value_cents").is_not_null()).height / max(
        sale.height, 1
    )
    cost_known = sale.filter(pl.col("product_cost_cents").is_not_null())

    def pct_of_list(cents: int) -> str:
        return f"{cents / gross_list:.1%}" if gross_list else "-"

    waterfall_rows = [
        {
            "stage": "Gross list value",
            "amount": _fmt_dollars(gross_list),
            "pct_of_list": "100.0%",
            "coverage": f"{list_coverage:.1%} of lines have an effective list",
        },
        {
            "stage": "Invoice revenue",
            "amount": _fmt_dollars(int(sale["invoice_revenue_cents"].sum())),
            "pct_of_list": pct_of_list(int(sale["invoice_revenue_cents"].sum())),
            "coverage": "all sale lines",
        },
        {
            "stage": "Pocket revenue",
            "amount": _fmt_dollars(int(sale["pocket_revenue_cents"].sum())),
            "pct_of_list": pct_of_list(int(sale["pocket_revenue_cents"].sum())),
            "coverage": "all sale lines",
        },
        {
            "stage": "Pocket gross profit",
            "amount": _fmt_dollars(int(cost_known["pocket_gp_cents"].sum())),
            "pct_of_list": pct_of_list(int(cost_known["pocket_gp_cents"].sum())),
            "coverage": f"{cost_known.height / max(sale.height, 1):.1%} of lines have cost",
        },
        {
            "stage": "Contribution",
            "amount": _fmt_dollars(int(cost_known["contribution_cents"].sum())),
            "pct_of_list": pct_of_list(int(cost_known["contribution_cents"].sum())),
            "coverage": "cost-to-serve = allocated freight + claims",
        },
    ]
    g = guidance_res.guidance
    guidance_summary = (
        g.group_by("guidance_status")
        .len()
        .sort("len", descending=True)
        .with_columns((pl.col("len") / g.height).alias("share"))
    )
    treatment_counts = (
        dict(opps.group_by("volume_treatment").len().iter_rows()) if opps.height else {}
    )
    ce = cts.customer_economics

    def _cust_rows(frame: pl.DataFrame) -> list[dict[str, str]]:
        return [
            {
                "customer_id": r["customer_id"],
                "net_revenue": _fmt_dollars(r["net_cents"]),
                "sales_contribution": _fmt_dollars(r["sales_contribution_cents"]),
                "returns_contribution": _fmt_dollars(r["returns_contribution_cents"]),
                "contribution": _fmt_dollars(r["contribution_cents"]),
                "activity_cost": _fmt_dollars(r["activity_cts_cents"]),
                "after_activity": _fmt_dollars(r["contribution_after_activity_cents"]),
                "pct_revenue_excluded": f"{r['pct_revenue_excluded']:.1%}",
            }
            for r in frame.to_dicts()
        ]

    ce_ranked = ce.sort(["contribution_after_activity_cents", "customer_id"])
    total_net = int(ce["net_cents"].sum())
    total_covered = int(ce["revenue_with_cost_cents"].sum())
    economics = {
        "n_orders": f"{cts.order_economics.height:,}",
        "n_customers": f"{ce.height:,}",
        "sales_contribution": _fmt_dollars(int(ce["sales_contribution_cents"].sum())),
        "returns_contribution": _fmt_dollars(int(ce["returns_contribution_cents"].sum())),
        "net_contribution": _fmt_dollars(int(ce["contribution_cents"].sum())),
        "activity_cost": _fmt_dollars(int(ce["activity_cts_cents"].sum())),
        "after_activity": _fmt_dollars(int(ce["contribution_after_activity_cents"].sum())),
        "pct_revenue_excluded": f"{1 - total_covered / total_net:.1%}" if total_net else "-",
        "top_customers": _cust_rows(ce_ranked.tail(5).reverse()),
        "bottom_customers": _cust_rows(ce_ranked.head(5)),
        "n_service_flags": cts.service_flags.height,
    }
    context: dict[str, Any] = {
        "manifest": manifest,
        "coverage": {
            "n_lines": f"{res.ledger.height:,}",
            "n_observations": f"{res.observations.height:,}",
            "net_revenue": _fmt_dollars(net_rev),
            "missing_cost_revenue_share": f"{missing_cost_rev / max(net_rev, 1):.2%}",
            "blank_rows_dropped": sum(res.blank_rows_dropped.values()),
            "library_warnings": lib_warnings,
        },
        "reconciliation": [
            {
                "check": label_of(str(c["check"])),
                "check_desc": describe(str(c["check"])),
                "expected": _fmt_dollars(c.get("expected_cents")),
                "actual": _fmt_dollars(c.get("actual_cents")),
                "allocated": _fmt_dollars(c.get("allocated_cents")),
                "residual": _fmt_dollars(c.get("residual_cents")),
                "source_total": _fmt_dollars(c.get("source_total_cents")),
                "pass": c["pass"],
            }
            for c in res.reconciliation.checks
        ],
        "residuals": [
            {
                "residual": label_of(k),
                "residual_desc": describe(k),
                "amount": _fmt_dollars(v),
                **_RESIDUAL_EXPLANATIONS.get(
                    k,
                    {
                        "what": "Residual recorded by the pipeline.",
                        "why": "Kept visible rather than forced onto lines.",
                        "where": "reconciliation.csv.",
                    },
                ),
            }
            for k, v in res.reconciliation.residuals.items()
            if v
        ],
        "unallocated_settlements": [
            {
                "customer_id": r["customer_id"],
                "period": r["period"],
                "rebate_program_id": r["rebate_program_id"],
                "amount": _fmt_dollars(r["amount_cents"]),
            }
            for r in exports["unallocated_settlements"].to_dicts()
        ],
        "notes": res.notes + seg.notes + guidance_res.notes,
        "waterfall": waterfall_rows,
        "dispersion": {
            "n_groups": peer_stats.height,
            "n_data_quality": int(flagged["flag_data_quality_anomaly"].sum()),
            "n_commercial": int(flagged["flag_commercial_pricing_outlier"].sum()),
        },
        "segments": {
            # The raw rule string is dropped here on purpose: the report shows
            # the plain-English name, description and condition bullets; raw
            # rule logic lives only in the CSV/Parquet exports.
            "rules": [
                {
                    **{k: v for k, v in r.items() if k != "rule"},
                    "display_rank": rank,
                    "name": plain_rule(str(r["rule"])).name,
                    "plain": plain_rule(str(r["rule"])).description,
                    "conditions": plain_rule(str(r["rule"])).conditions,
                    "technical_note": plain_rule(str(r["rule"])).note,
                }
                for rank, r in enumerate(
                    sorted(seg.rules, key=lambda r: (-int(r["n_lines"]), int(r["leaf_id"]))),
                    start=1,
                )
            ],
            "bootstrap_ari": f"{seg.bootstrap_median_ari:.3f}"
            if seg.bootstrap_median_ari is not None
            else "-",
            "rec_change": f"{seg.recommendation_change_share:.1%}"
            if seg.recommendation_change_share is not None
            else "-",
        },
        "sensitivity": [
            {
                **r,
                "status_label": label_of(str(r["status"])),
                "status_desc": describe(str(r["status"])),
            }
            for r in sens.to_dicts()
        ],
        "guidance_summary": [
            {
                "status": label_of(r["guidance_status"]),
                "status_desc": describe(r["guidance_status"]),
                "n": r["len"],
                "share": f"{r['share']:.1%}",
            }
            for r in guidance_summary.to_dicts()
        ],
        "guidance_label": label_of(PROVISIONAL_LABEL).lower(),
        "inventory": (
            {
                "method": res.mapping.lines.unit_cost.cost_basis.value
                if res.mapping.lines.unit_cost
                else "-",
                "n_sales_costed": inventory_cogs.height,
                "n_flagged": inventory_cogs.filter(pl.col("cogs_flag") != "").height,
                "cogs_total": _fmt_dollars(int(inventory_cogs["cogs_cents"].sum())),
            }
            if inventory_cogs is not None
            else None
        ),
        "economics": economics,
        "win_curves": (
            [
                {
                    **r,
                    "status_label": label_of(str(r["status"])),
                    "status_desc": describe(str(r["status"])),
                }
                for r in win_curves.to_dicts()
            ]
            if win_curves is not None
            else None
        ),
        "cross_estimates": (
            [
                {
                    **r,
                    "status_label": label_of(str(r["status"])),
                    "status_desc": describe(str(r["status"])),
                }
                for r in cross_estimates.to_dicts()
            ]
            if cross_estimates is not None
            else None
        ),
        "opportunity": {
            "leakage_total": _fmt_dollars(int(leak["total_leakage_cents"].sum())),
            "gross_total": _fmt_dollars(
                int(opps["gross_price_opportunity_cents"].sum()) if opps.height else 0
            ),
            "gross_conservative_total": _fmt_dollars(
                int(opps["gross_price_opportunity_conservative_cents"].fill_null(0).sum())
                if opps.height and "gross_price_opportunity_conservative_cents" in opps.columns
                else 0
            ),
            "expected_total": _fmt_dollars(
                int(opps["expected_contribution_change_cents"].fill_null(0).sum())
                if opps.height
                else 0
            ),
            "n_records": opps.height,
            "treatment_counts": ", ".join(
                f"{label_of(str(k))}: {v}" for k, v in sorted(treatment_counts.items())
            )
            or "-",
        },
        "tieout": (
            {
                "rows": [
                    {
                        **r,
                        "engine": _fmt_dollars(r["engine_cents"]),
                        "gl": _fmt_dollars(r["gl_cents"]),
                        "diff": _fmt_dollars(r["diff_cents"]),
                    }
                    for r in tieout_res.table.to_dicts()
                ],
                "n_failing": tieout_res.n_failing,
                "cost_coverage": f"{tieout_res.cost_coverage_share:.1%}",
            }
            if tieout_res is not None
            else None
        ),
        "real_validation": (
            {
                "backtest": real_val.backtest.to_dicts(),
                "placebo": real_val.placebo.to_dicts(),
                "stab_el": (
                    real_val.stability_elasticity.group_by("segment")
                    .agg(
                        pl.len().alias("n_windows"),
                        pl.col("fe_ols").min().alias("beta_min"),
                        pl.col("fe_ols").max().alias("beta_max"),
                        pl.col("fe_ols").std().alias("beta_sd"),
                    )
                    .sort("segment")
                    .to_dicts()
                    if real_val.stability_elasticity.height
                    else []
                ),
                "stab_seg": real_val.stability_segments.to_dicts(),
            }
            if real_val is not None
            else None
        ),
    }
    rss_log("report_context_built")
    render_report(context, out_dir / "report.html")
    rss_log("report_rendered")
    return FullRunResult(
        pipeline=res,
        flagged=flagged,
        segments=seg,
        guidance=guidance_res.guidance,
        sensitivity=sens,
        opportunities=opps,
        leakage=leak,
        timings=timings,
        out_dir=out_dir,
        panel=panel,
        margin_tree=margin_tree,
        peer_stats=peer_stats,
        order_economics=cts.order_economics,
        customer_economics=cts.customer_economics,
        service_flags=cts.service_flags,
        win_curves=win_curves,
        cross_estimates=cross_estimates,
        inventory_cogs=inventory_cogs,
        inventory_identity=inventory_identity,
        tieout=tieout_res,
        unallocated_settlements=exports["unallocated_settlements"],
        library_warnings=lib_warnings,
        manifest=manifest,
        fiscal=fiscal,
        actions=actions_res.actions,
        action_events=actions_res.events,
    )
