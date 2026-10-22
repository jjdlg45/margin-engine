"""Instrument builder (Run 4, 27.6): build the two instruments from data a
real company actually has, instead of ready-made shock columns.

- Supplier cost index from purchase/receipt history: the quantity-weighted
  average purchase cost per supplier per month, in logs, demeaned per supplier
  (a pair-FE-friendly shock series). Gaps are forward-filled within a supplier
  (the last observed cost level persists until a new receipt) and labeled.
- Category tariff index: the existing effective-dated tariff-rate table is
  already the instrument; this module only validates its shape.
- External index option (Phase 29): a public cost series (e.g. BLS PPI)
  mapped to item categories by a config table, then to suppliers through each
  supplier's modal category, producing the same supplier x month shape.

The engine's exclusion-rationale requirement is unchanged: whichever way an
instrument is BUILT, config must still document why it is excludable before
the top evidence status can be awarded.
"""

from __future__ import annotations

import polars as pl

from margin_engine.ingest.mapping import MappingError


def build_supplier_cost_index(
    receipts: pl.DataFrame, items: pl.DataFrame
) -> tuple[pl.DataFrame, list[str]]:
    """(supplier_id, period, cost_idx) from receipt history.

    Mix-corrected: log average purchase cost is demeaned PER ITEM first, then
    quantity-averaged within supplier x month. Averaging raw costs across a
    supplier's items would let month-to-month changes in which items were
    bought (mix) swamp the few-percent cost movements the instrument needs.
    log(cost_idx) is the within-supplier log cost deviation - exactly the
    z_cost the panel builder takes from a mapped supplier_cost_index table.
    """
    notes: list[str] = []
    required = {"item_id", "receipt_date", "quantity", "unit_cost_cents"}
    missing = required - set(receipts.columns)
    if missing:
        msg = (
            f"cannot build a supplier cost index: inventory_receipts lacks "
            f"{sorted(missing)}. Map those columns or provide a "
            f"supplier_cost_index table directly."
        )
        raise MappingError(msg)
    if "supplier_id" not in items.columns:
        msg = (
            "cannot build a supplier cost index: the items table maps no "
            "supplier_id, so receipts cannot be attributed to suppliers."
        )
        raise MappingError(msg)

    r = receipts.join(items.select("item_id", "supplier_id"), on="item_id", how="left")
    n_unmatched = r.filter(pl.col("supplier_id").is_null()).height
    if n_unmatched:
        notes.append(
            f"supplier cost index: {n_unmatched} receipt row(s) reference items "
            "with no supplier and were excluded from the index"
        )
        r = r.filter(pl.col("supplier_id").is_not_null())
    item_monthly = (
        r.filter((pl.col("quantity") > 0) & (pl.col("unit_cost_cents") > 0))
        .with_columns(pl.col("receipt_date").dt.strftime("%Y-%m").alias("period"))
        .group_by("supplier_id", "item_id", "period")
        .agg(
            (
                (pl.col("unit_cost_cents") * pl.col("quantity")).sum() / pl.col("quantity").sum()
            ).alias("avg_cost"),
            pl.col("quantity").sum().alias("_q"),
        )
        .with_columns(
            (pl.col("avg_cost").log() - pl.col("avg_cost").log().mean().over("item_id")).alias(
                "item_z"
            )
        )
    )
    monthly = (
        item_monthly.group_by("supplier_id", "period")
        .agg(((pl.col("item_z") * pl.col("_q")).sum() / pl.col("_q").sum()).alias("log_cost"))
        .sort("supplier_id", "period")
    )
    # Dense supplier x month grid so gaps are visible, then forward-fill.
    periods = monthly.select("period").unique()
    suppliers = monthly.select("supplier_id").unique()
    grid = suppliers.join(periods, how="cross").sort("supplier_id", "period")
    dense = grid.join(monthly, on=["supplier_id", "period"], how="left")
    n_gaps = dense.filter(pl.col("log_cost").is_null()).height
    dense = dense.with_columns(
        pl.col("log_cost").forward_fill().over("supplier_id").alias("log_cost")
    ).filter(pl.col("log_cost").is_not_null())
    if n_gaps:
        notes.append(
            f"supplier cost index: {n_gaps} supplier-month gap(s) carried the last "
            "observed cost forward (labeled; months before a supplier's first "
            "receipt are excluded)"
        )
    out = (
        dense.with_columns(
            (pl.col("log_cost") - pl.col("log_cost").mean().over("supplier_id")).alias("_z")
        )
        .with_columns(pl.col("_z").exp().alias("cost_idx"))
        .select("supplier_id", "period", "cost_idx")
        .sort("supplier_id", "period")
    )
    notes.append(
        f"supplier cost index built from {receipts.height:,} receipt rows "
        f"({out['supplier_id'].n_unique()} suppliers x {periods.height} months); "
        "exclusion rationale must still be documented in config"
    )
    return out, notes


def external_cost_index(
    series: pl.DataFrame,
    category_series_map: dict[str, str],
    items: pl.DataFrame,
) -> tuple[pl.DataFrame, list[str]]:
    """(supplier_id, period, cost_idx) from an external public series.

    series: (series_id, period, value) - e.g. BLS PPI levels. Each item
    category maps to one series via config; a supplier takes its modal
    category's series. value is logged and demeaned per series.
    """
    notes: list[str] = []
    required = {"series_id", "period", "value"}
    missing = required - set(series.columns)
    if missing:
        msg = f"external index series lacks columns {sorted(missing)}"
        raise MappingError(msg)
    if not category_series_map:
        msg = (
            "external cost index requested but sensitivity.external_index_map is "
            "empty: map each item_category to a series_id in config."
        )
        raise MappingError(msg)
    if {"supplier_id", "item_category"} - set(items.columns):
        msg = "external cost index needs items with supplier_id and item_category"
        raise MappingError(msg)

    modal = (
        items.group_by("supplier_id", "item_category")
        .agg(pl.len().alias("n"))
        .sort(["supplier_id", "n", "item_category"], descending=[False, True, False])
        .group_by("supplier_id", maintain_order=True)
        .agg(pl.col("item_category").first())
    )
    unmapped = sorted(set(modal["item_category"].to_list()) - set(category_series_map.keys()))
    if unmapped:
        msg = (
            f"external cost index: categories {unmapped} have no series in "
            f"sensitivity.external_index_map. Map every category explicitly; "
            f"the engine never guesses."
        )
        raise MappingError(msg)
    modal = modal.with_columns(
        pl.col("item_category")
        .replace_strict(category_series_map, return_dtype=pl.String)
        .alias("series_id")
    )
    z = (
        series.with_columns(pl.col("value").cast(pl.Float64).log().alias("log_v"))
        .with_columns((pl.col("log_v") - pl.col("log_v").mean().over("series_id")).alias("_z"))
        .with_columns(pl.col("_z").exp().alias("cost_idx"))
        .select("series_id", "period", "cost_idx")
    )
    out = (
        modal.join(z, on="series_id", how="inner")
        .select("supplier_id", "period", "cost_idx")
        .sort("supplier_id", "period")
    )
    notes.append(
        f"external cost index: {len(set(category_series_map.values()))} public "
        "series mapped to suppliers via modal category; industry-wide movement "
        "also moves competitor prices (see limitations) - rationale in config"
    )
    return out, notes
