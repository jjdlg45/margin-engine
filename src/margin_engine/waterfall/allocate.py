"""Exact allocation of group-level amounts to lines.

Method (spec 8.2): largest-remainder (Hamilton). Each line's exact share
amount x w_i / W is floored to the cent; the remaining cents are handed out
one at a time to the lines with the largest fractional remainders.
Deterministic tie-break: larger absolute weight first, then source_line_key.

Properties:
- allocations always sum exactly to the source amount, and
- no line deviates from its exact (unrounded) share by a cent or more.

Signed weights are supported (settlement rebates allocate over sales AND
returns, with returns carrying negative shares): flooring toward -inf keeps
every fractional remainder in [0, 1) regardless of sign, so the same
distribution step applies.
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
    """Allocate `group_amounts[amount_col]` across lines of each group by the
    largest-remainder method.

    lines: must contain group_key, driver_col (Int64/Float64) and
    source_line_key (tie-break). group_amounts: one row per group with
    amount_col in cents. Lines in groups with no amount get 0. Groups with zero
    total driver fall back to equal split. With signed_driver=True weights keep
    their sign; a group whose signed driver sum is <= 0 is left unallocated
    (the caller reports the residual) rather than forced.
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
    work = work.with_columns(pl.col("_w").sum().over(group_key).alias("_w_sum"))
    if signed_driver:
        # Unallocatable groups (driver sum <= 0): zero the amount; residual is
        # total minus allocated, computed by the caller.
        work = work.with_columns(
            pl.when(pl.col("_w_sum") <= 0).then(0).otherwise(pl.col("_grp_amt")).alias("_grp_amt")
        ).with_columns(
            pl.when(pl.col("_w_sum") <= 0).then(0).otherwise(pl.col("_w")).alias("_w"),
            pl.when(pl.col("_w_sum") <= 0).then(1).otherwise(pl.col("_w_sum")).alias("_w_sum"),
        )
    else:
        # Zero-driver groups: equal weights.
        work = work.with_columns(
            pl.when(pl.col("_w_sum") == 0).then(1).otherwise(pl.col("_w")).alias("_w"),
        ).with_columns(pl.col("_w").sum().over(group_key).alias("_w_sum"))

    # Hamilton: floor of the exact share (floordiv floors toward -inf, so the
    # integer remainder is in [0, W) for any sign), then give the leftover
    # cents to the largest remainders.
    work = work.with_columns(
        (pl.col("_grp_amt") * pl.col("_w")).floordiv(pl.col("_w_sum")).alias("_floor_share"),
        ((pl.col("_grp_amt") * pl.col("_w")) % pl.col("_w_sum")).alias("_rem"),
    )
    work = work.with_columns(
        (pl.col("_grp_amt") - pl.col("_floor_share").sum().over(group_key)).alias("_leftover"),
        # Rank within group: remainder desc, |weight| desc, source_line_key asc.
        pl.int_range(pl.len())
        .sort_by(
            [-pl.col("_rem"), -pl.col("_w").abs(), pl.col("source_line_key")],
        )
        .arg_sort()
        .over(group_key)
        .alias("_rank"),
    )
    work = work.with_columns(
        (pl.col("_floor_share") + (pl.col("_rank") < pl.col("_leftover")).cast(pl.Int64)).alias(
            out_col
        )
    )
    return work.drop("_grp_amt", "_w", "_w_sum", "_floor_share", "_rem", "_leftover", "_rank")
