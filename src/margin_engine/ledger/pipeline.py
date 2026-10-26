"""Phase 2 pipeline: mapping -> ingest -> classify -> waterfall -> reconcile -> store.

The single entry point other phases build on. Returns both views plus the
reconciliation report; optionally persists everything to DuckDB with an audit
trail back to source line keys.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import polars as pl

from margin_engine.config import Rules
from margin_engine.ingest.fx import convert_lines, convert_reference_amounts, quarter_end_rates
from margin_engine.ingest.loader import load_lines, load_reference_table
from margin_engine.ingest.mapping import MappingError, SourceMapping, load_mapping
from margin_engine.memlog import rss_log
from margin_engine.transactions.classify import ClassifiedData, classify, sales_observations
from margin_engine.waterfall.engine import WaterfallResult, build_waterfall
from margin_engine.waterfall.reconcile import ReconciliationReport, reconcile


@dataclass
class PipelineResult:
    mapping: SourceMapping
    ledger: pl.DataFrame  # waterfall lines, financial ledger view
    observations: pl.DataFrame  # clean sales for behavioral estimation
    removed_duplicates: pl.DataFrame
    reconciliation: ReconciliationReport
    unallocated_settlements: pl.DataFrame | None
    reference: dict[str, pl.DataFrame | None]
    notes: list[str]
    # Per customer x year x program tiered summary (Run 6, Phase 61), for the
    # economic-event taxonomy and the rebate subledger.
    tiered_summary: pl.DataFrame | None = None
    # Per customer x program x month tiered accrual stream (Run 6, Phase 62).
    tiered_monthly: pl.DataFrame | None = None
    # Fully blank input rows dropped per source file (whitespace-only included).
    blank_rows_dropped: dict[str, int] = field(default_factory=dict)


# Low-cardinality code and status columns carried on every ledger line. Stored
# as Categorical in memory (Phase 35): the dictionary replaces a per-row string
# copy — the long cost_build_label alone is ~60 bytes a line — while polars 2
# keeps comparisons, group-bys, sorts and concat_str lexically identical.
# Exports cast back to String, so every file is byte-identical to before.
CODE_COLUMNS = [
    "transaction_type",
    "return_reason",
    "credit_class",
    "credit_reason",
    "kit_role",
    "cost_build_label",
    "uom",
    "base_uom",
    "uom_norm",
    "currency",
    "channel",
    "sales_rep_id",
    "rebate_program_id",
    "contract_id",
    "supplier_id",
    "inbound_freight_per_unit",
    "duty_per_unit",
    "return_recovery_status",
    "return_freight_exp_status",
    "return_handling_status",
    "billed_charge_status",
    "rebate_settlement_status",
    "tiered_rebate_status",
    "freight_expense_status",
]


def _compact_codes(frame: pl.DataFrame) -> pl.DataFrame:
    casts = [
        pl.col(c).cast(pl.Categorical)
        for c in CODE_COLUMNS
        if c in frame.columns and frame.schema[c] == pl.String
    ]
    return frame.with_columns(casts) if casts else frame


REFERENCE_TABLES = [
    "price_list",
    "uom_conversions",
    "customers",
    "items",
    "contracts",
    "rebate_programs",
    "rebate_settlements",
    "order_charges",
    "freight_log",
    "vendor_rebate_programs",
    "landed_cost_components",
    "supplier_cost_index",
    "tariff_schedule",
    "return_freight_log",
    "fx_rates",
    "rebate_program_tiers",
    "tiered_settlements",
    "inventory_receipts",
    "quotes",
    "substitution_groups",
    "market_price_index",
    "gl_totals",
    "external_cost_series",
    "fiscal_periods",
]


def run_pipeline(
    data_dir: Path,
    mapping_path: Path,
    rules: Rules,
    *,
    lines: pl.DataFrame | None = None,
    cache_dir: Path | None = None,
) -> PipelineResult:
    """`lines` lets a caller hand in an already-canonical lines frame (the
    replay feed filters a cached parse instead of re-parsing every tick);
    `cache_dir` enables the fingerprint-keyed parse cache (Phase 35). Both off
    by default; results are identical either way."""
    mapping = load_mapping(mapping_path)
    blank_counts: dict[str, int] = {}
    if lines is None and cache_dir is not None:
        from margin_engine.cache import StageCache, fingerprint_file

        cache = StageCache(cache_dir)
        key = cache.key(
            "canonical_lines",
            [fingerprint_file(data_dir / mapping.lines_file), fingerprint_file(mapping_path)],
        )
        lines = cache.get(key)
        if lines is None:
            lines = load_lines(data_dir, mapping, blank_counts)
            cache.put(key, lines, meta=blank_counts)
        else:
            blank_counts.update(cache.get_meta(key) or {})
            rss_log("load_lines_cache_hit")
    if lines is None:
        lines = load_lines(data_dir, mapping, blank_counts)
    rss_log("load_lines")
    ref = {
        name: load_reference_table(data_dir, mapping, name, blank_counts)
        for name in REFERENCE_TABLES
    }

    # Multi-currency: convert line-currency amounts to the reporting currency
    # (invoice-month rate); settlements at the quarter's last-month rate and
    # order charges at the order month's rate follow the customer / order
    # currency derived from the lines.
    fx_res = convert_lines(lines, mapping, ref["fx_rates"], rules)
    lines = fx_res.lines
    if fx_res.converted and ref["fx_rates"] is not None:
        reporting = rules.units_and_currency.reporting_currency
        cust_cur = lines.group_by("customer_id").agg(pl.col("currency").first().alias("currency"))
        multi = lines.group_by("customer_id").agg(pl.col("currency").n_unique().alias("n"))
        if multi.filter(pl.col("n") > 1).height:
            bad = multi.filter(pl.col("n") > 1)["customer_id"].head(10).to_list()
            msg = (
                f"customers invoiced in multiple currencies: {bad}; settlement "
                f"conversion needs one currency per customer"
            )
            raise MappingError(msg)
        if ref["rebate_settlements"] is not None:
            ref["rebate_settlements"] = convert_reference_amounts(
                ref["rebate_settlements"],
                cust_cur,
                "customer_id",
                ref["fx_rates"],
                reporting,
                period_col="period",
                period_rate=quarter_end_rates(ref["fx_rates"]),
            ).drop("currency", strict=False)
        if ref["order_charges"] is not None and "order_id" in lines.columns:
            order_cur = lines.group_by("order_id").agg(
                pl.col("currency").first().alias("currency"),
                pl.col("invoice_date").dt.strftime("%Y-%m").first().alias("period"),
            )
            charges = ref["order_charges"].join(
                order_cur.select("order_id", "period"), on="order_id", how="left"
            )
            ref["order_charges"] = convert_reference_amounts(
                charges,
                order_cur.select("order_id", "currency"),
                "order_id",
                ref["fx_rates"],
                reporting,
                period_col="period",
            ).drop("currency", "period", strict=False)

    classified: ClassifiedData = classify(lines, rules)
    rss_log("classify")
    del lines  # consumed; keep only one wide frame alive at 10M-line scale (27.2)
    wf: WaterfallResult = build_waterfall(
        classified.ledger,
        mapping,
        rules=rules,
        price_list=ref["price_list"],
        uom_conversions=ref["uom_conversions"],
        items=ref["items"],
        order_charges=ref["order_charges"],
        freight_log=ref["freight_log"],
        rebate_settlements=ref["rebate_settlements"],
        rebate_programs=ref["rebate_programs"],
        vendor_rebate_programs=ref["vendor_rebate_programs"],
        landed_cost_components=ref["landed_cost_components"],
        return_freight_log=ref["return_freight_log"],
        rebate_program_tiers=ref["rebate_program_tiers"],
        tiered_settlements=ref["tiered_settlements"],
    )
    rss_log("waterfall")
    wf.lines = _compact_codes(wf.lines)
    # The classified ledger is superseded by the waterfall lines; only the
    # small duplicate frame and source totals are still needed (27.2).
    classified.ledger = pl.DataFrame()
    report = reconcile(
        wf.lines,
        classified.removed_duplicates,
        classified.source_totals,
        wf.residuals,
        source_totals_by_currency=fx_res.source_totals_by_currency if fx_res.converted else None,
        order_charges=ref["order_charges"],
        freight_log=ref["freight_log"],
        rebate_settlements=ref["rebate_settlements"],
        return_freight_log=ref["return_freight_log"],
        tiered_settlements=ref["tiered_settlements"],
    )
    obs = sales_observations(wf.lines)
    rss_log("observations")
    skipped = [name for name in REFERENCE_TABLES if ref[name] is None]
    notes = list(wf.notes) + fx_res.notes
    if mapping.lines.order_id is None:
        notes.append(
            "no order_id in the mapping: the invoice is used as the order unit "
            "(order economics, frequency ABC, freight allocation) - a labeled proxy"
        )
    anon = wf.lines.filter((pl.col("transaction_type") == "sale") & pl.col("customer_id").is_null())
    if anon.height:
        sale_rev = int(
            wf.lines.filter(pl.col("transaction_type") == "sale")["net_amount_cents"].sum()
        )
        share = int(anon["net_amount_cents"].sum()) / sale_rev if sale_rev else 0.0
        notes.append(
            f"{anon.height:,} sale line(s) ({share:.1%} of sale revenue) carry no "
            "customer identity: kept in the ledger and totals, excluded from "
            "customer-level behavior views (observations, segmentation, panel)"
        )
    if skipped:
        notes.append(f"reference tables absent (features skipped): {skipped}")
    return PipelineResult(
        mapping=mapping,
        ledger=wf.lines,
        observations=obs,
        removed_duplicates=classified.removed_duplicates,
        reconciliation=report,
        unallocated_settlements=wf.unallocated_settlements,
        reference=ref,
        notes=notes,
        blank_rows_dropped=blank_counts,
        tiered_summary=wf.tiered_summary,
        tiered_monthly=wf.tiered_monthly,
    )


def store_duckdb(
    result: PipelineResult, db_path: Path, prewritten: dict[str, Path] | None = None
) -> None:
    """Persist both views and the audit trail. source_line_key ties every row
    in every table back to the original export line.

    `prewritten` maps export names to parquet files the caller already wrote
    (Phase 35): the largest tables load straight from those files instead of
    being serialized a second time. Table names stay the same either way."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    pre = prewritten or {}
    con = duckdb.connect(str(db_path))
    try:
        # Bounded working memory: DuckDB spills to disk under pressure instead
        # of ballooning the engine process at the largest table loads (Phase 35).
        con.execute("SET memory_limit='1GB'")
        frames: dict[str, pl.DataFrame] = {
            "financial_ledger": result.ledger,
            "sales_observations": result.observations,
            "removed_duplicates": result.removed_duplicates,
            "reconciliation_report": result.reconciliation.to_frame(),
        }
        # Export-name -> store-name for tables that can reuse written parquet.
        reusable = {
            "financial_ledger": "financial_ledger",
            "reconciliation": "reconciliation_report",
        }
        for name, ref_df in result.reference.items():
            if ref_df is not None:
                frames[f"ref_{name}"] = ref_df
        frames["reconciliation_residuals"] = pl.DataFrame(
            {
                "residual": list(result.reconciliation.residuals.keys()),
                "amount_cents": list(result.reconciliation.residuals.values()),
            }
        )
        done: set[str] = set()
        for export_name, table in reusable.items():
            path = pre.get(export_name)
            if path is not None and path.exists():
                con.execute(
                    f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM read_parquet(?)',
                    [str(path)],
                )
                done.add(table)
        if "financial_ledger" in done:
            # The observations view is a pure row filter of the ledger (5.4):
            # derive it inside DuckDB from the table just loaded instead of
            # serializing the frame a second time.
            con.execute(
                'CREATE OR REPLACE TABLE "sales_observations" AS '
                'SELECT * FROM "financial_ledger" '
                "WHERE transaction_type = 'sale' AND quantity > 0 "
                "AND net_amount_cents > 0 AND customer_id IS NOT NULL"
            )
            done.add("sales_observations")
        # Hand frames to DuckDB via parquet files (polars-native writer), not
        # arrow registration: pyarrow can be unavailable in locked-down
        # environments and parquet keeps the exact Int64 cents types.
        with tempfile.TemporaryDirectory() as td:
            for name, df in frames.items():
                if name in done or not df.columns:
                    continue
                pq = Path(td) / f"{name}.parquet"
                df.write_parquet(pq)
                con.execute(
                    f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM read_parquet(?)',
                    [str(pq)],
                )
    finally:
        con.close()
