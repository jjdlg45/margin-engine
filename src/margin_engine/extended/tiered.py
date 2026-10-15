"""Tiered retrospective rebates (Phase 11), engine side.

Accrual rule (documented, mirrored by the generator): each sale/return line of
a tiered-program customer accrues at the rate of the tier its cumulative
calendar-year net (including the line) sits in — status `estimated`. When a
retrospective settlement row exists for the customer-year, the settled amount
is allocated over that year's lines (largest remainder, signed weights) and
the waterfall USES the actual — status `actual`; the accrual stays visible and
the true-up (actual - accrued) is reported. Measured in reporting currency.
"""

from __future__ import annotations

import polars as pl

from margin_engine.money import div_half_even_expr
from margin_engine.waterfall.allocate import allocate_exact

ZERO_COLS = [
    pl.lit(0, dtype=pl.Int64).alias("tiered_accrued_cents"),
    pl.lit(0, dtype=pl.Int64).alias("tiered_actual_alloc_cents"),
    pl.lit(0, dtype=pl.Int64).alias("tiered_rebate_used_cents"),
    pl.lit(None, dtype=pl.String).alias("tiered_rebate_status"),
    pl.lit(0, dtype=pl.Int64).alias("tiered_rate_bp"),
]


def apply_tiered_rebates(
    lines: pl.DataFrame,
    rebate_programs: pl.DataFrame | None,
    tiers: pl.DataFrame | None,
    settlements: pl.DataFrame | None,
    notes: list[str],
    residuals: dict[str, int],
) -> pl.DataFrame:
    if rebate_programs is None or tiers is None:
        return lines.with_columns(ZERO_COLS)
    tiered_cust = rebate_programs.filter(pl.col("program_kind") == "tiered").select(
        "customer_id", pl.col("rebate_program_id").alias("_pgm")
    )
    if tiered_cust.height == 0:
        return lines.with_columns(ZERO_COLS)

    in_basis = (
        pl.col("transaction_type").is_in(["sale", "return"])
        & (pl.col("net_amount_cents") != 0)
        & pl.col("customer_id").is_in(tiered_cust["customer_id"].implode())
    )
    basis = lines.filter(in_basis)
    other = lines.filter(~in_basis).with_columns(ZERO_COLS)
    if basis.height == 0:
        return lines.with_columns(ZERO_COLS)

    # Cumulative calendar-year net per customer, deterministic ordering.
    basis = (
        basis.with_columns(pl.col("invoice_date").dt.year().alias("_year"))
        .sort("customer_id", "_year", "invoice_date", "source_line_key")
        .with_columns(
            pl.col("net_amount_cents").cum_sum().over("customer_id", "_year").alias("_cum")
        )
        .join(tiered_cust, on="customer_id", how="left")
    )
    # Tier rate: largest tier whose minimum the cumulative net has reached.
    # The engine's tier table uses "min_amount_cents"; tiers are per program.
    tier_rates = (
        basis.select("source_line_key", "_pgm", "_cum")
        .join(
            tiers.select(
                pl.col("rebate_program_id").alias("_pgm"),
                pl.col("min_amount_cents"),
                (pl.col("rate_pct") * 100).round(0).cast(pl.Int64).alias("rate_bp"),
            ),
            on="_pgm",
        )
        .filter(pl.col("_cum") >= pl.col("min_amount_cents"))
        .sort("min_amount_cents")
        .group_by("source_line_key", maintain_order=True)
        .agg(pl.col("rate_bp").last())
    )
    basis = basis.join(tier_rates, on="source_line_key", how="left").with_columns(
        pl.col("rate_bp").fill_null(0).alias("tiered_rate_bp")
    )
    basis = basis.with_columns(
        div_half_even_expr(pl.col("net_amount_cents") * pl.col("tiered_rate_bp"), 10_000).alias(
            "tiered_accrued_cents"
        )
    )

    # Retrospective settlements: customer-year actuals allocated over the year.
    settled_keys: set[str] = set()
    if settlements is not None and settlements.height:
        settle = (
            settlements.with_columns(
                (pl.col("customer_id") + "|" + pl.col("period").str.slice(0, 4)).alias("_ty_key")
            )
            .group_by("_ty_key")
            .agg(pl.col("amount_cents").sum())
        )
        basis = basis.with_columns(
            (pl.col("customer_id") + "|" + pl.col("_year").cast(pl.String)).alias("_ty_key")
        )
        basis = allocate_exact(
            basis,
            settle,
            "_ty_key",
            "amount_cents",
            "net_amount_cents",
            "tiered_actual_alloc_cents",
            signed_driver=True,
        )
        settled_keys = set(settle["_ty_key"].to_list())
        matched = set(basis["_ty_key"].to_list())
        unmatched = settle.filter(~pl.col("_ty_key").is_in(sorted(matched)))
        residuals["tiered_settlements_unallocated_cents"] = int(unmatched["amount_cents"].sum())
        basis = basis.with_columns(
            pl.col("_ty_key").is_in(sorted(settled_keys)).alias("_settled")
        ).drop("_ty_key")
    else:
        basis = basis.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("tiered_actual_alloc_cents"),
            pl.lit(False).alias("_settled"),
        )
        notes.append("no tiered settlements table: tiered rebates remain accrual estimates")

    basis = basis.with_columns(
        pl.when(pl.col("_settled"))
        .then(pl.col("tiered_actual_alloc_cents"))
        .otherwise(pl.col("tiered_accrued_cents"))
        .alias("tiered_rebate_used_cents"),
        pl.when(pl.col("_settled"))
        .then(pl.lit("actual"))
        .otherwise(pl.lit("estimated"))
        .alias("tiered_rebate_status"),
    )
    settled_lines = basis.filter(pl.col("_settled"))
    true_up = int(settled_lines["tiered_actual_alloc_cents"].sum()) - int(
        settled_lines["tiered_accrued_cents"].sum()
    )
    residuals["tiered_true_up_cents"] = true_up
    basis = basis.drop("_year", "_cum", "_pgm", "rate_bp", "_settled")
    return pl.concat([other, basis], how="diagonal_relaxed")
