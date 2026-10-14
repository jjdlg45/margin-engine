"""Transaction classification and the two views (spec 5.3 / 5.4).

Input: canonical lines from the loader. Output:
  - ledger frame: every surviving row, classified and flagged
  - removed duplicates audit (verified identity only)
  - sales-observations view (clean sale lines for behavioral estimation)

Excluding a row from observations NEVER removes its dollars from the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from margin_engine.config import Rules

BUSINESS_COLS = [
    "invoice_id",
    "invoice_date",
    "customer_id",
    "item_id",
    "quantity",
    "net_amount_cents",
    "transaction_type",
]


@dataclass
class ClassifiedData:
    ledger: pl.DataFrame
    removed_duplicates: pl.DataFrame
    source_totals: dict[str, int]


def classify(lines: pl.DataFrame, rules: Rules) -> ClassifiedData:
    """Classify, dedupe on verified identity, flag everything else."""
    source_totals = {
        "rows": lines.height,
        "net_amount_cents": int(lines["net_amount_cents"].fill_null(0).sum()),
    }

    # --- verified duplicates: same source key AND identical business content ---
    value_cols = list(lines.columns)
    dup_keys = lines.group_by("source_line_key").len().filter(pl.col("len") > 1)
    removed = lines.clear()
    if dup_keys.height:
        in_dup = lines.join(dup_keys.select("source_line_key"), on="source_line_key", how="semi")
        rest = lines.join(dup_keys.select("source_line_key"), on="source_line_key", how="anti")
        # Identical across ALL columns -> verified duplicate: keep first, remove rest.
        in_dup = in_dup.with_columns(pl.int_range(pl.len()).over(value_cols).alias("_copy_num"))
        removed = in_dup.filter(pl.col("_copy_num") > 0).drop("_copy_num")
        kept_from_dups = in_dup.filter(pl.col("_copy_num") == 0).drop("_copy_num")
        # Same key but different content: key collision -> keep all, flag.
        collision_keys = kept_from_dups.group_by("source_line_key").len().filter(pl.col("len") > 1)
        kept_from_dups = kept_from_dups.with_columns(
            pl.col("source_line_key")
            .is_in(collision_keys["source_line_key"].implode())
            .alias("flag_key_collision")
        )
        lines = pl.concat(
            [rest.with_columns(pl.lit(False).alias("flag_key_collision")), kept_from_dups],
            how="vertical",
        )
    else:
        lines = lines.with_columns(pl.lit(False).alias("flag_key_collision"))

    # --- look-alike duplicates: identical business content, different key ------
    lines = lines.with_columns((pl.len().over(BUSINESS_COLS) > 1).alias("flag_lookalike_duplicate"))

    # --- credit classification -------------------------------------------------
    price_reasons = list(rules.transactions.price_credit_reasons)
    cost_reasons = list(rules.transactions.cost_credit_reasons)
    lines = lines.with_columns(
        pl.when(pl.col("transaction_type") != "credit")
        .then(pl.lit(None, dtype=pl.String))
        .when(pl.col("credit_reason").is_in(price_reasons))
        .then(pl.lit("price_credit"))
        .when(pl.col("credit_reason").is_in(cost_reasons))
        .then(pl.lit("cost_credit"))
        .otherwise(pl.lit("unclassified_credit"))
        .alias("credit_class")
    )

    # --- data flags (5.3) -------------------------------------------------------
    lines = lines.with_columns(
        (pl.col("unit_cost_scaled").is_null() & (pl.col("transaction_type") == "sale")).alias(
            "flag_missing_cost"
        ),
        ((pl.col("unit_cost_scaled") == 0) & (pl.col("transaction_type") == "sale")).alias(
            "flag_zero_cost"
        ),
        ((pl.col("quantity") == 0) & (pl.col("transaction_type") == "sale")).alias(
            "flag_zero_quantity"
        ),
        ((pl.col("quantity") < 0) & (pl.col("transaction_type") == "sale")).alias(
            "flag_negative_qty_sale"
        ),
        (pl.col("net_amount_cents") <= 0).alias("flag_nonpositive_net"),
        pl.col("list_price_scaled").is_null().alias("flag_missing_list_price")
        if "list_price_scaled" in lines.columns
        else pl.lit(True).alias("flag_missing_list_price"),
    )
    return ClassifiedData(ledger=lines, removed_duplicates=removed, source_totals=source_totals)


def sales_observations(ledger: pl.DataFrame) -> pl.DataFrame:
    """Clean sale lines for estimating purchase behavior (5.4 view 1).

    Excludes returns, credits, zero/negative quantity, nonpositive net.
    Financial effects of the excluded rows remain in the ledger.
    """
    return ledger.filter(
        (pl.col("transaction_type") == "sale")
        & (pl.col("quantity") > 0)
        & (pl.col("net_amount_cents") > 0)
    )
