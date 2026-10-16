"""Full Run-1 pipeline orchestration (Phase 8).

ingest -> classify -> waterfall -> reconcile -> dispersion -> segmentation ->
guidance -> panel -> sensitivity -> opportunity -> outputs (parquet/csv),
HTML report, run manifest, DuckDB store. Stage timings recorded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from margin_engine.config import load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.extended.cost_to_serve import run_cost_to_serve
from margin_engine.extended.hierarchy import apply_analysis_level
from margin_engine.guidance.bands import PROVISIONAL_LABEL, build_guidance
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline, store_duckdb
from margin_engine.manifest.run_manifest import (
    build_manifest,
    cleaning_decisions_from,
    write_manifest,
)
from margin_engine.money import cents_col_to_dollars
from margin_engine.opportunity.sizing import observed_leakage, price_opportunities
from margin_engine.panel.build import build_panel
from margin_engine.report.render import render_report
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


def _fmt_dollars(cents: int | float | None) -> str:
    if cents is None:
        return "-"
    return f"{cents / 100:,.0f}"


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
) -> FullRunResult:
    rules = load_rules(rules_path)
    timings: dict[str, float] = {}
    out_dir.mkdir(parents=True, exist_ok=True)

    t = time.perf_counter()
    res = run_pipeline(data_dir, mapping_path, rules)
    timings["ingest_classify_waterfall_reconcile"] = time.perf_counter() - t

    t = time.perf_counter()
    cts = run_cost_to_serve(res.ledger, rules)
    res.ledger = cts.lines
    res.notes.extend(cts.notes)
    timings["cost_to_serve"] = time.perf_counter() - t

    t = time.perf_counter()
    analysis_obs, h_notes = apply_analysis_level(
        res.observations, res.reference["customers"], rules
    )
    res.notes.extend(h_notes)
    flagged, peer_stats, _abc = run_dispersion(analysis_obs, res.reference["items"], rules)
    timings["dispersion_outliers_abc"] = time.perf_counter() - t

    t = time.perf_counter()
    df = prepare_segmentation_frame(flagged, res.reference["customers"], res.reference["items"])
    seg = fit_commercial_segments(df, rules, seed=0)
    df = df.join(seg.leaf_assignments, on="source_line_key", how="left")
    margin_tree = fit_margin_driver_tree(df, rules, seed=0)
    timings["segmentation"] = time.perf_counter() - t

    t = time.perf_counter()
    guidance_res = build_guidance(df, rules, res.reference["rebate_programs"])
    timings["guidance"] = time.perf_counter() - t

    t = time.perf_counter()
    panel = build_panel(
        df,
        res.reference["items"],
        res.reference["supplier_cost_index"],
        res.reference["tariff_schedule"],
        rules,
    )
    timings["panel"] = time.perf_counter() - t

    t = time.perf_counter()
    sens = run_sensitivity(panel, rules)
    timings["sensitivity"] = time.perf_counter() - t

    t = time.perf_counter()
    leak = observed_leakage(res.ledger, rules)
    opps = price_opportunities(guidance_res.guidance, panel, sens, rules)
    timings["opportunity"] = time.perf_counter() - t

    # --- optional Run-2 modules: each runs when its input exists, otherwise
    # switches off with a stated reason -------------------------------------
    t = time.perf_counter()
    inventory_cogs: pl.DataFrame | None = None
    inventory_identity: pl.DataFrame | None = None
    if res.reference["inventory_receipts"] is not None:
        from margin_engine.extended.inventory import cost_sales

        res.pipeline_cost_basis if hasattr(res, "pipeline_cost_basis") else None
        method = {"standard": "standard", "average": "average", "fifo": "fifo"}.get(
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

    cross_estimates: pl.DataFrame | None = None
    if res.reference["substitution_groups"] is not None:
        from margin_engine.extended.substitution import estimate_cross_effects

        cross_estimates = estimate_cross_effects(panel, res.reference["substitution_groups"], rules)
        res.notes.append("cross-price estimation ran within declared substitution groups")
    else:
        res.notes.append("cross-price estimation OFF: no substitution_groups table available")
    timings["optional_modules"] = time.perf_counter() - t

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
    }
    exports["unallocated_settlements"] = (
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
    exports = {
        name: _round_solver_outputs(name, _natural_sort(name, frame))
        for name, frame in exports.items()
    }
    for fmt in rules.reporting.output_formats:
        if fmt == "parquet":
            for name, frame in exports.items():
                frame.write_parquet(out_dir / f"{name}.parquet")
        elif fmt == "csv":
            for name, frame in exports.items():
                csv_frame = frame
                for col, dtype in frame.schema.items():
                    if col.endswith("_cents") and dtype == pl.Int64:
                        csv_frame = csv_frame.with_columns(
                            cents_col_to_dollars(col).alias(col.replace("_cents", "_usd"))
                        ).drop(col)
                try:
                    csv_frame.write_csv(out_dir / f"{name}.csv")
                except pl.exceptions.PolarsError:
                    csv_frame.with_columns(pl.col(pl.Date).cast(pl.String)).write_csv(
                        out_dir / f"{name}.csv"
                    )
    store_duckdb(res, out_dir / "engine.duckdb")
    timings["exports"] = time.perf_counter() - t

    # ---- manifest -----------------------------------------------------------
    manifest = build_manifest(
        data_dir=data_dir,
        mapping_path=mapping_path,
        rules_path=rules_path,
        cleaning_decisions=cleaning_decisions_from(res.ledger, res.removed_duplicates),
        parameters={
            "n_segments": seg.leaf_assignments["segment_leaf"].n_unique(),
            "bootstrap_median_ari": seg.bootstrap_median_ari,
            "notes": res.notes + seg.notes + guidance_res.notes,
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
        },
        "reconciliation": res.reconciliation.checks,
        "residuals": [
            {"residual": k, "amount_cents": v} for k, v in res.reconciliation.residuals.items() if v
        ],
        "notes": res.notes + seg.notes + guidance_res.notes,
        "waterfall": waterfall_rows,
        "dispersion": {
            "n_groups": peer_stats.height,
            "n_data_quality": int(flagged["flag_data_quality_anomaly"].sum()),
            "n_commercial": int(flagged["flag_commercial_pricing_outlier"].sum()),
        },
        "segments": {
            "rules": seg.rules,
            "bootstrap_ari": f"{seg.bootstrap_median_ari:.3f}"
            if seg.bootstrap_median_ari is not None
            else "-",
            "rec_change": f"{seg.recommendation_change_share:.1%}"
            if seg.recommendation_change_share is not None
            else "-",
        },
        "sensitivity": sens.to_dicts(),
        "guidance_summary": [
            {"status": r["guidance_status"], "n": r["len"], "share": f"{r['share']:.1%}"}
            for r in guidance_summary.to_dicts()
        ],
        "guidance_label": PROVISIONAL_LABEL,
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
        "win_curves": win_curves.to_dicts() if win_curves is not None else None,
        "cross_estimates": cross_estimates.to_dicts() if cross_estimates is not None else None,
        "opportunity": {
            "leakage_total": _fmt_dollars(int(leak["total_leakage_cents"].sum())),
            "gross_total": _fmt_dollars(
                int(opps["gross_price_opportunity_cents"].sum()) if opps.height else 0
            ),
            "expected_total": _fmt_dollars(
                int(opps["expected_contribution_change_cents"].fill_null(0).sum())
                if opps.height
                else 0
            ),
            "n_records": opps.height,
            "treatment_counts": treatment_counts,
        },
    }
    render_report(context, out_dir / "report.html")
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
    )
