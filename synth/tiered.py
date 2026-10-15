"""Tiered retrospective rebates (Phase 11): planted truth.

Program: annual (calendar-year) measurement, three volume tiers with rising
rates. During the year each line accrues at the rate of the tier the customer's
cumulative year-to-date net (including the line) sits in. At year end the
retrospective settlement is final-tier-rate x full-year net; the true-up is the
difference to the accruals. Tiers and settlements are measured in the
reporting currency (program-level economics), so this runs BEFORE any local
currency conversion of the export.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from synth.money import apply_bp

TIER_RATES_BP = [100, 200, 300]


def plant_tiered_rebates(
    lines: pl.DataFrame, rebate_programs: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Returns (lines + truth columns, tiers table, settlements table).

    Thresholds are the 50th and 85th percentiles of tiered customers'
    customer-year nets — deterministic per world, exported with the program.
    """
    tiered = rebate_programs.filter(pl.col("program_kind") == "tiered")
    empty_tiers = pl.DataFrame(
        schema={"rebate_program_id": pl.String, "min_cents": pl.Int64, "rate_bp": pl.Int64}
    )
    empty_settle = pl.DataFrame(
        schema={
            "customer_id": pl.String,
            "period": pl.String,
            "rebate_program_id": pl.String,
            "amount_cents": pl.Int64,
        }
    )
    lines = lines.with_columns(
        pl.lit(0, dtype=pl.Int64).alias("truth_tiered_accrual_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_tiered_rate_bp"),
    )
    if tiered.height == 0:
        return lines, empty_tiers, empty_settle

    eligible = (
        lines.join(tiered.select("customer_id", "rebate_program_id"), on="customer_id", how="inner")
        .filter(pl.col("txn_type").is_in(["sale", "return"]))
        .with_columns(pl.col("invoice_date").dt.year().alias("_year"))
    )
    if eligible.height == 0:
        return lines, empty_tiers, empty_settle
    cy = eligible.group_by("customer_id", "_year").agg(pl.col("net_cents").sum().alias("cy_net"))
    nets = cy["cy_net"].to_numpy()
    t1 = int(np.quantile(nets, 0.50))
    t2 = int(np.quantile(nets, 0.85))
    tiers = pl.DataFrame(
        {
            "rebate_program_id": ["TIER01"] * 3,
            "min_cents": [0, t1, t2],
            "rate_bp": TIER_RATES_BP,
        }
    )

    def rate_for(cum: pl.Expr) -> pl.Expr:
        return (
            pl.when(cum >= t2)
            .then(TIER_RATES_BP[2])
            .when(cum >= t1)
            .then(TIER_RATES_BP[1])
            .otherwise(TIER_RATES_BP[0])
            .cast(pl.Int64)
        )

    # Accrual rule: rate of the tier of cumulative YTD net INCLUDING the line.
    acc = (
        eligible.sort("customer_id", "_year", "invoice_date", "line_seq")
        .with_columns(pl.col("net_cents").cum_sum().over("customer_id", "_year").alias("_cum"))
        .with_columns(rate_for(pl.col("_cum")).alias("truth_tiered_rate_bp"))
    )
    acc = acc.with_columns(
        pl.Series(
            "truth_tiered_accrual_cents",
            apply_bp(acc["net_cents"].to_numpy(), acc["truth_tiered_rate_bp"].to_numpy()),
        )
    )
    # Settlement: final-tier rate x full-year net (retrospective).
    settle = (
        acc.group_by("customer_id", "_year")
        .agg(pl.col("net_cents").sum().alias("cy_net"))
        .with_columns(rate_for(pl.col("cy_net")).alias("final_rate_bp"))
    )
    settle = (
        settle.with_columns(
            pl.Series(
                "amount_cents",
                apply_bp(settle["cy_net"].to_numpy(), settle["final_rate_bp"].to_numpy()),
            ),
            (pl.col("_year").cast(pl.String) + "-FY").alias("period"),
            pl.lit("TIER01").alias("rebate_program_id"),
        )
        .select("customer_id", "period", "rebate_program_id", "amount_cents")
        .sort("customer_id", "period")
    )

    lines = (
        lines.drop("truth_tiered_accrual_cents", "truth_tiered_rate_bp")
        .join(
            acc.select("line_seq", "truth_tiered_accrual_cents", "truth_tiered_rate_bp"),
            on="line_seq",
            how="left",
        )
        .with_columns(
            pl.col("truth_tiered_accrual_cents").fill_null(0),
            pl.col("truth_tiered_rate_bp").fill_null(0),
        )
    )
    return lines, tiers, settle
