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
    # Fully blank input rows dropped per source file (whitespace-only included).
    blank_rows_dropped: dict[str, int] = field(default_factory=dict)


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
]


def run_pipeline(data_dir: Path, mapping_path: Path, rules: Rules) -> PipelineResult:
    mapping = load_mapping(mapping_path)
    blank_counts: dict[str, int] = {}
    lines = load_lines(data_dir, mapping, blank_counts)
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
    skipped = [name for name in REFERENCE_TABLES if ref[name] is None]
    notes = list(wf.notes) + fx_res.notes
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
    )


def store_duckdb(result: PipelineResult, db_path: Path) -> None:
    """Persist both views and the audit trail. source_line_key ties every row
    in every table back to the original export line."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        frames: dict[str, pl.DataFrame] = {
            "financial_ledger": result.ledger,
            "sales_observations": result.observations,
            "removed_duplicates": result.removed_duplicates,
            "reconciliation_report": result.reconciliation.to_frame(),
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
        # Hand frames to DuckDB via parquet files (polars-native writer), not
        # arrow registration: pyarrow can be unavailable in locked-down
        # environments and parquet keeps the exact Int64 cents types.
        with tempfile.TemporaryDirectory() as td:
            for name, df in frames.items():
                if not df.columns:
                    continue
                pq = Path(td) / f"{name}.parquet"
                df.write_parquet(pq)
                con.execute(
                    f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM read_parquet(?)',
                    [str(pq)],
                )
    finally:
        con.close()
