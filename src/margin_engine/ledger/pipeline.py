"""Phase 2 pipeline: mapping -> ingest -> classify -> waterfall -> reconcile -> store.

The single entry point other phases build on. Returns both views plus the
reconciliation report; optionally persists everything to DuckDB with an audit
trail back to source line keys.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import polars as pl

from margin_engine.config import Rules
from margin_engine.ingest.loader import load_lines, load_reference_table
from margin_engine.ingest.mapping import SourceMapping, load_mapping
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
    reference: dict[str, pl.DataFrame | None]
    notes: list[str]


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
]


def run_pipeline(data_dir: Path, mapping_path: Path, rules: Rules) -> PipelineResult:
    mapping = load_mapping(mapping_path)
    lines = load_lines(data_dir, mapping)
    ref = {name: load_reference_table(data_dir, mapping, name) for name in REFERENCE_TABLES}

    classified: ClassifiedData = classify(lines, rules)
    wf: WaterfallResult = build_waterfall(
        classified.ledger,
        mapping,
        price_list=ref["price_list"],
        uom_conversions=ref["uom_conversions"],
        items=ref["items"],
        order_charges=ref["order_charges"],
        freight_log=ref["freight_log"],
        rebate_settlements=ref["rebate_settlements"],
        rebate_programs=ref["rebate_programs"],
        vendor_rebate_programs=ref["vendor_rebate_programs"],
        landed_cost_components=ref["landed_cost_components"],
    )
    report = reconcile(
        wf.lines,
        classified.removed_duplicates,
        classified.source_totals,
        wf.residuals,
        order_charges=ref["order_charges"],
        freight_log=ref["freight_log"],
        rebate_settlements=ref["rebate_settlements"],
    )
    obs = sales_observations(wf.lines)
    skipped = [name for name in REFERENCE_TABLES if ref[name] is None]
    notes = list(wf.notes)
    if skipped:
        notes.append(f"reference tables absent (features skipped): {skipped}")
    return PipelineResult(
        mapping=mapping,
        ledger=wf.lines,
        observations=obs,
        removed_duplicates=classified.removed_duplicates,
        reconciliation=report,
        reference=ref,
        notes=notes,
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
