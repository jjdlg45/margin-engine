"""Exact allocation of group-level amounts to lines.

Every allocation: integer cents, proportional to a driver, half-even rounded,
with the rounding residual assigned deterministically to the largest line in
the group (spec 8.2), so allocated lines sum exactly to the source amount.
Each allocated column is accompanied by a status: actual | allocated | estimated.
"""

from __future__ import annotations

import polars as pl


def allocate_exact(
    lines: pl.DataFrame,
    group_amounts: pl.DataFrame,
    group_key: str,
    amount_col: str,
    driver_col: str,
    out_col: str,
    *,
    signed_driver: bool = False,
) -> pl.DataFrame:
    """Allocate `group_amounts[amount_col]` across lines of each group.

    lines: must contain group_key and driver_col (Int64/Float64).
    group_amounts: one row per group with amount_col in cents.
    Lines in groups with no amount get 0. Groups with zero total driver fall
    back to equal split. With signed_driver=True weights keep their sign
    (returns receive negative shares); a group whose signed driver sum is <= 0
    is left unallocated (the caller reports the residual) rather than forced.
    Returns lines with out_col added.
    """
    work = lines.join(
        group_amounts.select(pl.col(group_key), pl.col(amount_col).alias("_grp_amt")),
        on=group_key,
        how="left",
    ).with_columns(pl.col("_grp_amt").fill_null(0))

    # Integer driver weights (scale floats to 1e4 to stay in integer math).
    driver_dtype = work.schema[driver_col]
    w = (
        (pl.col(driver_col).fill_null(0) * 10_000).round(0).cast(pl.Int64)
        if driver_dtype == pl.Float64
        else pl.col(driver_col).fill_null(0).cast(pl.Int64)
    )
    work = work.with_columns((w if signed_driver else w.abs()).alias("_w"))
    work = work.with_columns(
        pl.col("_w").sum().over(group_key).alias("_w_sum"),
        pl.len().over(group_key).alias("_n"),
    )
    if signed_driver:
        # Unallocatable groups (driver sum <= 0): zero the amount; residual is
        # total minus allocated, computed by the caller.
        work = work.with_columns(
            pl.when(pl.col("_w_sum") <= 0).then(0).otherwise(pl.col("_grp_amt")).alias("_grp_amt")
        )
        work = work.with_columns(
            pl.when(pl.col("_w_sum") <= 0).then(0).otherwise(pl.col("_w")).alias("_w"),
            pl.when(pl.col("_w_sum") <= 0).then(1).otherwise(pl.col("_w_sum")).alias("_w_sum"),
        )
    else:
        # Zero-driver groups: equal weights.
        work = work.with_columns(
            pl.when(pl.col("_w_sum") == 0).then(1).otherwise(pl.col("_w")).alias("_w"),
        ).with_columns(pl.col("_w").sum().over(group_key).alias("_w_sum"))

    # Raw share, floor division; residual cents to the largest-weight line.
    work = work.with_columns(
        (pl.col("_grp_amt") * pl.col("_w")).floordiv(pl.col("_w_sum")).alias("_share")
    )
    work = work.with_columns(pl.col("_share").sum().over(group_key).alias("_share_sum"))
    work = work.with_columns((pl.col("_grp_amt") - pl.col("_share_sum")).alias("_resid"))
    # Deterministic largest line: max weight, ties broken by first occurrence.
    work = work.with_columns(pl.int_range(pl.len()).alias("_rowno"))
    work = work.with_columns(
        pl.col("_rowno")
        .sort_by([pl.col("_w"), pl.col("_rowno")], descending=[True, False])
        .first()
        .over(group_key)
        .alias("_winner")
    )
    work = work.with_columns(
        (
            pl.col("_share")
            + pl.when(pl.col("_rowno") == pl.col("_winner")).then(pl.col("_resid")).otherwise(0)
        ).alias(out_col)
    )
    return work.drop(
        "_grp_amt", "_w", "_w_sum", "_n", "_share", "_share_sum", "_resid", "_rowno", "_winner"
    )
