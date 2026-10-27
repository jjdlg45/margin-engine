"""Full Run-1 pipeline orchestration (Phase 8).

ingest -> classify -> waterfall -> reconcile -> dispersion -> segmentation ->
guidance -> panel -> sensitivity -> opportunity -> outputs (parquet/csv),
HTML report, run manifest, DuckDB store. Stage timings recorded.
"""

from __future__ import annotations

import hashlib
import re
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from margin_engine.actions.generate import generate_actions
from margin_engine.actions.portfolio import build_portfolio_groups
from margin_engine.compliance import run_robinson_patman
from margin_engine.config import load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.extended.cost_to_serve import run_cost_to_serve
from margin_engine.extended.hierarchy import apply_analysis_level
from margin_engine.fiscal import FiscalCalendar, build_calendar
from margin_engine.guidance.bands import PROVISIONAL_LABEL, build_guidance
from margin_engine.labels import describe, plain_rule
from margin_engine.labels import label as label_of
from margin_engine.ledger.events import build_events, vendor_rebate_rollforward
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline, store_duckdb
from margin_engine.ledger.rebate_subledger import build_rebate_subledger
from margin_engine.manifest.run_manifest import (
    _sha256 as file_sha256,
)
from margin_engine.manifest.run_manifest import (
    build_manifest,
    cleaning_decisions_from,
    write_manifest,
)
from margin_engine.manifest.run_manifest import (
    fingerprint_inputs as manifest_inputs,
)
from margin_engine.memlog import rss_log
from margin_engine.money import cents_col_to_dollars
from margin_engine.opportunity.deductions import build_tiered_context, deduction_coverage
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
from margin_engine.sensitivity.estimate import annotate_placebo, run_sensitivity_full
from margin_engine.sensitivity.market_control import select_market_index


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
    # Overlap-group portfolio estimates (Run 6, Phase 68).
    action_portfolio: pl.DataFrame | None = None
    # Long-form causal-gate record per segment (Run 6, Phase 70).
    sensitivity_gates: pl.DataFrame | None = None


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
    "action_portfolio_groups": ["overlap_group"],
    "sensitivity": ["segment"],
    "sensitivity_gates": ["segment", "gate_order"],
    "opportunities": ["customer_id", "item_id"],
    "order_economics": ["order_id"],
    "customer_economics": ["customer_id"],
    "service_policy_flags": ["customer_id"],
    "inventory_cogs": ["source_line_key"],
    "inventory_identity": ["item_id"],
    "win_curves": ["segment"],
    "cross_price_estimates": ["group_id", "item_a", "item_b"],
    "unallocated_settlements": ["customer_id", "period", "rebate_program_id"],
    "economic_events": ["event_type", "event_subtype", "source_ref"],
    "economic_event_totals": ["event_type"],
    "vendor_rebate_rollforward": ["supplier_id", "period"],
    "rebate_subledger": ["customer_id", "rebate_program_id", "period", "entry_type", "source_ref"],
    "rebate_subledger_balances": ["customer_id", "rebate_program_id", "period"],
    "cost_pools": ["cost_pool_id", "detail"],
    "deduction_coverage": ["customer_id"],
    "gl_trace_sample": ["month", "source_line_key"],
    "books_tieout": ["month", "measure"],
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
    # Cost pools covered by ACTUAL expense sources in this run (Run 6, Phase
    # 63): the pool overlap check switches the per-delivery estimate off when
    # a freight log exists and refuses any other double-counted pool.
    actual_pools: set[str] = set()
    if res.reference.get("freight_log") is not None:
        actual_pools.add("outbound_freight")
    if res.reference.get("return_freight_log") is not None:
        actual_pools.add("return_freight")
    if (
        "claims_cost_alloc_cents" in res.ledger.columns
        and int(res.ledger["claims_cost_alloc_cents"].fill_null(0).abs().sum()) > 0
    ):
        actual_pools.add("claims")
    cts = run_cost_to_serve(res.ledger, rules, fiscal, frozenset(actual_pools))
    res.ledger = cts.lines
    res.notes.extend(cts.notes)
    timings["cost_to_serve"] = time.perf_counter() - t
    rss_log("cost_to_serve")

    # Economic event taxonomy (Run 6, Phase 61): every adjustment classified by
    # its economics, with counterparty, direction, periods, basis, status and
    # reversal links; plus the vendor rebate roll-forward (ASC 705-20).
    t = time.perf_counter()
    events_res = build_events(
        res.ledger,
        rules=rules,
        rebate_settlements=res.reference.get("rebate_settlements"),
        order_charges=res.reference.get("order_charges"),
        vendor_rebate_programs=res.reference.get("vendor_rebate_programs"),
        tiered_summary=res.tiered_summary,
        charge_declarations=res.mapping.charge_declarations,
    )
    res.notes.extend(events_res.notes)
    rollforward_res = vendor_rebate_rollforward(
        res.ledger,
        res.reference.get("inventory_receipts"),
        res.reference.get("items"),
        res.reference.get("vendor_rebate_programs"),
    )
    res.notes.extend(rollforward_res.notes)
    # Rebate lifecycle subledger (Run 6, Phase 62): estimate -> revisions ->
    # settlement -> reversal/true-up, with the balance invariant validated
    # (a violation raises; it is an accounting bug, never a warning).
    subledger_res = build_rebate_subledger(
        res.ledger,
        res.reference.get("rebate_programs"),
        res.reference.get("rebate_settlements"),
        res.tiered_monthly,
        res.tiered_summary,
    )
    res.notes.extend(subledger_res.notes)
    timings["economic_events"] = time.perf_counter() - t
    rss_log("economic_events")

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
    # K definition (Run 6, Phase 65 / Finding 8): the pair's per-unit
    # cost-to-serve over the covered baseline, decomposed so the optimizer can
    # treat order-level costs by the declared order-cost response. Variable
    # component: per-line and per-unit handling rates over observed lines and
    # units; order component: the remainder (per-order and per-delivery
    # allocations). Both derive from the POOL-RESOLVED effective rates, so a
    # rate switched off by the overlap check never re-enters through K.
    eff_rates = cts.rates or {}
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
            pl.len().alias("_n_lines"),
        )
        .filter(pl.col("_qty") > 0)
        .with_columns(
            (pl.col("_cts") / pl.col("_qty")).round(0).cast(pl.Int64).alias("cts_per_unit_cents"),
            (
                (
                    pl.col("_n_lines") * eff_rates.get("per_line", 0)
                    + pl.col("_qty") * eff_rates.get("special_handling", 0)
                )
                / pl.col("_qty")
            )
            .round(0)
            .cast(pl.Int64)
            .alias("k_variable_per_unit_cents"),
        )
        .with_columns(
            (pl.col("cts_per_unit_cents") - pl.col("k_variable_per_unit_cents"))
            .clip(lower_bound=0)
            .alias("k_order_per_unit_cents")
        )
        .select(
            "customer_id",
            "item_id",
            "cts_per_unit_cents",
            "k_variable_per_unit_cents",
            "k_order_per_unit_cents",
        )
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
    # Public-data-only rule (Phase 37) + competitor-path control selection
    # (Run 6, Phase 70), in one shared function so tests and the validation
    # scripts exercise the same decision production does.
    mpi_spec = res.mapping.reference_tables.get("market_price_index")
    market_idx, mkt_source, mkt_notes = select_market_index(
        res.reference.get("market_price_index"),
        public_source=mpi_spec.public_source if mpi_spec is not None else None,
        rules=rules,
    )
    res.notes.extend(mkt_notes)
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
        # Zero-row price sources (Run 6, Phase 72). Both are optional reference
        # tables; absent them the ladder falls back to carrying prices forward,
        # which is what it did before.
        quotes=res.reference.get("quotes"),
        price_list=res.reference.get("price_list"),
    )
    timings["panel"] = time.perf_counter() - t
    rss_log("panel")

    t = time.perf_counter()
    # The index joins at category x month; the gate compares that grain to
    # each instrument's declared variation (Run 6, Phase 70).
    sens_res = run_sensitivity_full(
        panel,
        rules,
        market_control_source=mkt_source,
        market_control_grain="item_category:month",
    )
    sens, sens_gates = sens_res.estimates, sens_res.gates
    timings["sensitivity"] = time.perf_counter() - t
    rss_log("sensitivity")

    t = time.perf_counter()
    leak = observed_leakage(res.ledger, rules, fiscal)
    # Deductions with explicit denominators (Run 6, Phase 66): tiered programs
    # are RECOMPUTED at candidate revenue, never flattened into d; a tiered
    # customer without a tier table gets the fixed-rate approximation label.
    tiered_cust_ids: set[str] = set()
    _programs_ref = res.reference.get("rebate_programs")
    if _programs_ref is not None:
        tiered_cust_ids = {
            str(c)
            for c in _programs_ref.filter(pl.col("program_kind") == "tiered")[
                "customer_id"
            ].to_list()
        }
    tiered_ctx = build_tiered_context(
        res.reference.get("rebate_programs"),
        res.reference.get("rebate_program_tiers"),
        res.tiered_summary,
    )
    if tiered_cust_ids and tiered_ctx is None:
        res.notes.append(
            f"{len(tiered_cust_ids)} tiered-program customer(s) have no usable tier "
            "table: their opportunity figures use the flat deduction rate, labeled "
            "a fixed-rate approximation"
        )
    opps = price_opportunities(
        guidance_res.guidance,
        panel,
        sens,
        rules,
        tiered_ctx=tiered_ctx,
        tiered_customers=tiered_cust_ids,
    )
    ded_coverage = deduction_coverage(guidance_res.guidance, tiered_cust_ids)
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
        # The shuffled-instrument placebo speaks to whether this design's
        # inference is correctly sized, which is read beside the elasticity it
        # qualifies rather than only in the validation appendix (Run 6, Phase
        # 71). It runs after estimation, so it is annotated on, never a gate.
        sens = annotate_placebo(sens, real_val.placebo)
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

    # Monthly GL variance check (27.3; reframed as a control in Run 6 Phase
    # 64): runs only when a gl_totals table is mapped; its absence is a stated
    # status, never an implied pass.
    tieout_res = None
    gl_totals = res.reference.get("gl_totals")
    if gl_totals is not None:
        tieout_res = run_tieout(res.ledger, gl_totals, rules, fiscal)
        res.notes.extend(tieout_res.notes)
        res.notes.append(
            f"monthly GL variance check ran: {tieout_res.n_failing} month-measure(s) "
            f"outside tolerance"
            if tieout_res.n_failing
            else "monthly GL variance check ran: every compared month-measure within tolerance"
        )
    else:
        res.notes.append(
            "monthly GL variance check not testable: no GL totals table in the "
            "mapping/data (source-total reconciliation remains exact and always on)"
        )

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
    # Staleness fingerprints (Run 6, Phase 69): what this generation run rests
    # on. Input = the source files' content hashes; config = the rules hash;
    # model = the elasticity evidence the volume response came from, so a
    # proposal can be told apart from one generated under a different model.
    _fp_inputs = ";".join(f"{f['file']}:{f['sha256'][:12]}" for f in manifest_inputs(data_dir))
    fingerprints = {
        "input": hashlib.sha256(_fp_inputs.encode()).hexdigest()[:16],
        "config": file_sha256(rules_path)[:16],
        "model": hashlib.sha256(
            "|".join(
                f"{r['segment']}:{r['status']}:{r['iv'] if r['iv'] is not None else 'na'}"
                for r in sens.sort("segment").select("segment", "status", "iv").to_dicts()
            ).encode()
        ).hexdigest()[:16],
    }
    actions_res = generate_actions(
        ledger=res.ledger,
        guidance=guidance_res.guidance,
        opportunities=opps,
        leakage=leak,
        service_flags=cts.service_flags,
        sensitivity=sens,
        panel=panel,
        rules=rules,
        fingerprints=fingerprints,
    )
    res.notes.extend(actions_res.notes)
    # Portfolio scenarios for overlapping scopes (Run 6, Phase 68): one row
    # per overlap group with the naive sum and the joint estimate under a
    # common baseline - never summed into any headline.
    action_portfolio = build_portfolio_groups(
        actions_res.actions, panel, guidance_res.guidance, sens, rules
    )
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
        "action_portfolio_groups": action_portfolio,
        "sensitivity": sens,
        "sensitivity_gates": sens_gates,
        "opportunities": opps,
        "leakage": leak,
        "order_economics": cts.order_economics,
        **({"inventory_cogs": inventory_cogs} if inventory_cogs is not None else {}),
        **({"inventory_identity": inventory_identity} if inventory_identity is not None else {}),
        **({"win_curves": win_curves} if win_curves is not None else {}),
        **({"cross_price_estimates": cross_estimates} if cross_estimates is not None else {}),
        "customer_economics": cts.customer_economics,
        "service_policy_flags": cts.service_flags,
        "economic_events": events_res.events,
        "economic_event_totals": events_res.totals,
        "rebate_subledger": subledger_res.entries,
        "rebate_subledger_balances": subledger_res.balances,
        **({"cost_pools": cts.pools} if cts.pools is not None else {}),
        "deduction_coverage": ded_coverage,
        **(
            {"vendor_rebate_rollforward": rollforward_res.table}
            if rollforward_res.table is not None
            else {}
        ),
        "reconciliation": res.reconciliation.to_frame(),
        **({"books_tieout": tieout_res.table} if tieout_res is not None else {}),
        **(
            {"gl_trace_sample": tieout_res.trace_sample}
            if tieout_res is not None and tieout_res.trace_sample is not None
            else {}
        ),
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
    # Display casing for data values (Phase 46): acronyms from the mapping.
    _acronyms = frozenset(res.mapping.display.acronyms)
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
                    "name": plain_rule(str(r["rule"]), _acronyms).name,
                    "plain": plain_rule(str(r["rule"]), _acronyms).description,
                    "conditions": plain_rule(str(r["rule"]), _acronyms).conditions,
                    "technical_note": plain_rule(str(r["rule"]), _acronyms).note,
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
            "expected_gp_total": _fmt_dollars(
                int(opps["expected_pocket_gross_profit_change_cents"].fill_null(0).sum())
                if opps.height and "expected_pocket_gross_profit_change_cents" in opps.columns
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
                        "variance": _fmt_dollars(r["variance_cents"]),
                        "variance_abs": _fmt_dollars(r["variance_abs_cents"]),
                    }
                    for r in tieout_res.table.to_dicts()
                ],
                "n_failing": tieout_res.n_failing,
                "cost_coverage": f"{tieout_res.cost_coverage_share:.1%}",
                "status": label_of(tieout_res.status),
                "fiscal_mapping": tieout_res.fiscal_mapping,
                "account_mapping": tieout_res.account_mapping,
                "unresolved_items": tieout_res.unresolved_items,
                "tolerance": (
                    f"{rules.reporting.tieout_tolerance_pct}% or "
                    f"{_fmt_dollars(rules.reporting.tieout_tolerance_cents)}"
                ),
                "trace": (
                    [
                        {
                            **t,
                            "net": _fmt_dollars(t["net_amount_cents"]),
                            "cost": _fmt_dollars(t["product_cost_cents"]),
                            "period_engine": _fmt_dollars(t["period_engine_net_sales_cents"]),
                            "period_gl": _fmt_dollars(t["period_gl_net_sales_cents"]),
                        }
                        for t in tieout_res.trace_sample.to_dicts()
                    ]
                    if tieout_res.trace_sample is not None
                    else []
                ),
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
        sensitivity_gates=sens_gates,
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
        action_portfolio=action_portfolio,
    )
