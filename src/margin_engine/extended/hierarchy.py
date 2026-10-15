"""Customer hierarchy (Phase 10): ship-to -> bill-to -> parent account.

Analytics can run at a configurable level. The ledger always stays at ship-to
grain; this module swaps the analysis key on an analytics frame, keeping the
original ship-to id for audit. Missing hierarchy attributes fall back to the
ship-to id (a customer with no parent is its own parent), labeled in notes.
"""

from __future__ import annotations

import polars as pl

from margin_engine.config import Rules


def hierarchy_map(customers: pl.DataFrame | None) -> pl.DataFrame | None:
    """ship_to -> bill_to -> parent mapping from the customers reference."""
    if customers is None or "customer_id" not in (
        customers.columns if customers is not None else []
    ):
        return None
    cols = [pl.col("customer_id").alias("ship_to_id")]
    if "bill_to_id" in customers.columns:
        cols.append(
            pl.coalesce(pl.col("bill_to_id").replace("", None), pl.col("customer_id")).alias(
                "bill_to_id"
            )
        )
    else:
        cols.append(pl.col("customer_id").alias("bill_to_id"))
    if "parent_account_id" in customers.columns:
        cols.append(
            pl.coalesce(pl.col("parent_account_id").replace("", None), pl.col("customer_id")).alias(
                "parent_id"
            )
        )
    else:
        cols.append(pl.col("customer_id").alias("parent_id"))
    return customers.select(cols)


def apply_analysis_level(
    df: pl.DataFrame, customers: pl.DataFrame | None, rules: Rules
) -> tuple[pl.DataFrame, list[str]]:
    """Swap `customer_id` to the configured analysis level.

    The original ship-to id is preserved in `ship_to_customer_id`. At the
    default `ship_to` level the frame is returned unchanged.
    """
    level = rules.hierarchy.analysis_level
    if level == "ship_to":
        return df, []
    hmap = hierarchy_map(customers)
    if hmap is None:
        return df, [
            f"hierarchy analysis_level={level} requested but no customers reference "
            f"with hierarchy columns is available; staying at ship_to"
        ]
    key = "bill_to_id" if level == "bill_to" else "parent_id"
    out = (
        df.join(
            hmap.select("ship_to_id", key), left_on="customer_id", right_on="ship_to_id", how="left"
        )
        .with_columns(
            pl.col("customer_id").alias("ship_to_customer_id"),
            pl.coalesce(pl.col(key), pl.col("customer_id")).alias("customer_id"),
        )
        .drop(key)
    )
    return out, [f"analytics grouped at hierarchy level: {level} (ledger stays ship-to)"]
