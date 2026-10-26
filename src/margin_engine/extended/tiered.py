"""Tiered retrospective rebates (Phase 11; multi-program since Run 6 Phase 61).

Accrual rule (documented, mirrored by the generator): each sale/return line of
a tiered-program customer accrues, PER PROGRAM, at the rate of the tier its
cumulative calendar-year net (including the line) sits in — status `estimated`.
When a retrospective settlement row exists for the customer-year-program, the
settled amount is allocated over that year's lines (largest remainder, signed
weights) and the waterfall USES the actual for that program — status `actual`;
the accrual stays visible and the true-up (actual - accrued) is reported.
Measured in reporting currency.

Multiple simultaneous programs (Run 6, Phase 61): a customer may hold several
tiered programs with overlapping eligibility. Each program accrues on its own
tier table over the SAME cumulative basis; a line's columns carry the SUM
across programs, so no dollar is counted twice and no ledger row is ever
duplicated. Settlements are matched per (customer, year, program): a year
where only some programs settled uses actuals for those and accruals for the
rest (`partially_settled`). A settlement naming a program the customer does
not hold, or a year with no eligible lines, stays a visible residual — never
guessed onto lines.

A customer-year whose eligible basis is NEGATIVE (returns exceed purchases)
accrues nothing by construction (no tier threshold is reached by a negative
cumulative); the per-program summary marks it `negative_basis` so the
economic-event taxonomy can state the fail-closed outcome.

Phase 35: computed on a narrow projection (row id, customer, date, net,
transaction type) and joined back by the caller, instead of splitting the
full-width ledger. Values are identical to the pre-refactor path for
single-program customers.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from margin_engine.money import div_half_even_expr
from margin_engine.waterfall.allocate import allocate_exact

# Output columns in ledger order; rows outside the tiered basis default to
# zero cents / null status (the caller fills them after the join).
TIERED_COLS = [
    "tiered_accrued_cents",
    "tiered_actual_alloc_cents",
    "tiered_rebate_used_cents",
    "tiered_rebate_status",
    "tiered_rate_bp",
]

# Per customer x year x program: the grain of the economic-event taxonomy
# (Phase 61) and the rebate lifecycle subledger (Phase 62).
TIERED_SUMMARY_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "customer_id": pl.String,
    "year": pl.String,
    "rebate_program_id": pl.String,
    "eligible_basis_cents": pl.Int64,
    "accrued_cents": pl.Int64,
    "settled": pl.Boolean,
    "actual_alloc_cents": pl.Int64,
    "status": pl.String,
    "negative_basis": pl.Boolean,
}


@dataclass
class TieredResult:
    narrow: pl.DataFrame  # (row_col, *TIERED_COLS), one row per basis line
    summary: pl.DataFrame  # TIERED_SUMMARY_SCHEMA


def tiered_rebate_columns(
    lines: pl.DataFrame,
    rebate_programs: pl.DataFrame | None,
    tiers: pl.DataFrame | None,
    settlements: pl.DataFrame | None,
    notes: list[str],
    residuals: dict[str, int],
    *,
    row_col: str,
) -> TieredResult | None:
    """Narrow tiered-rebate computation.

    `lines` carries row_col, source_line_key, customer_id, invoice_date,
    net_amount_cents and transaction_type. Returns the per-line columns plus
    the per-(customer, year, program) summary, or None when the feature does
    not apply (no programs, no tiers, no tiered customers, or an empty basis).
    """
    if rebate_programs is None or tiers is None:
        return None
    tiered_cust = (
        rebate_programs.filter(pl.col("program_kind") == "tiered")
        .select("customer_id", pl.col("rebate_program_id").alias("_pgm"))
        .unique()
        .sort("customer_id", "_pgm")
    )
    if tiered_cust.height == 0:
        return None

    in_basis = (
        pl.col("transaction_type").is_in(["sale", "return"])
        & (pl.col("net_amount_cents") != 0)
        & pl.col("customer_id").is_in(tiered_cust["customer_id"].implode())
    )
    basis = lines.filter(in_basis)
    if basis.height == 0:
        return None

    # Cumulative calendar-year net per customer, deterministic ordering. The
    # cumulative basis is a property of the customer-year, shared by every
    # program the customer holds.
    basis = (
        basis.with_columns(pl.col("invoice_date").dt.year().cast(pl.String).alias("_year"))
        .sort("customer_id", "_year", "invoice_date", "source_line_key")
        .with_columns(
            pl.col("net_amount_cents").cum_sum().over("customer_id", "_year").alias("_cum")
        )
    )

    # One row per line x program the customer holds.
    per = basis.join(tiered_cust, on="customer_id", how="inner")
    # Tier rate PER PROGRAM: largest tier whose minimum the cumulative net has
    # reached, from that program's own tier table.
    tier_rates = (
        per.select(row_col, "_pgm", "_cum")
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
        .group_by(row_col, "_pgm", maintain_order=True)
        .agg(pl.col("rate_bp").last())
    )
    per = per.join(tier_rates, on=[row_col, "_pgm"], how="left").with_columns(
        pl.col("rate_bp").fill_null(0).alias("_rate_bp")
    )
    per = per.with_columns(
        div_half_even_expr(pl.col("net_amount_cents") * pl.col("_rate_bp"), 10_000).alias(
            "_accrued"
        ),
        (pl.col("customer_id") + "|" + pl.col("_year") + "|" + pl.col("_pgm")).alias("_typ_key"),
    )

    # Retrospective settlements: customer-year-program actuals allocated over
    # that year's lines by signed net share.
    if settlements is not None and settlements.height:
        if "rebate_program_id" in settlements.columns:
            pgm_expr = pl.col("rebate_program_id")
            settle_src = settlements
        else:
            # No program column: unambiguous only for customers holding exactly
            # one tiered program; a multi-program customer's settlement cannot
            # be attributed and stays a visible residual (fail closed).
            pgm_count = tiered_cust.group_by("customer_id").len()
            single = tiered_cust.join(
                pgm_count.filter(pl.col("len") == 1).select("customer_id"),
                on="customer_id",
                how="semi",
            )
            settle_src = settlements.join(
                single.rename({"_pgm": "rebate_program_id"}), on="customer_id", how="left"
            )
            pgm_expr = pl.col("rebate_program_id").fill_null("ambiguous_multi_program")
            if settle_src.filter(pl.col("rebate_program_id").is_null()).height:
                notes.append(
                    "tiered settlements carry no program id and at least one "
                    "customer holds several tiered programs: those settlements "
                    "cannot be attributed and stay a visible residual"
                )
        settle = (
            settle_src.with_columns(
                (
                    pl.col("customer_id") + "|" + pl.col("period").str.slice(0, 4) + "|" + pgm_expr
                ).alias("_typ_key")
            )
            .group_by("_typ_key")
            .agg(pl.col("amount_cents").sum())
        )
        per = allocate_exact(
            per,
            settle,
            "_typ_key",
            "amount_cents",
            "net_amount_cents",
            "_actual_alloc",
            signed_driver=True,
        )
        settled_keys = sorted(set(settle["_typ_key"].to_list()))
        per = per.with_columns(pl.col("_typ_key").is_in(settled_keys).alias("_settled"))
        matched = set(per["_typ_key"].to_list())
        unmatched = settle.filter(~pl.col("_typ_key").is_in(sorted(matched)))
        residuals["tiered_settlements_unallocated_cents"] = int(unmatched["amount_cents"].sum())
        if unmatched.height:
            notes.append(
                f"{unmatched.height} tiered settlement group(s) name a customer-"
                f"year-program with no eligible lines (or a program the customer "
                f"does not hold); kept as visible residual, never guessed onto lines"
            )
    else:
        per = per.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("_actual_alloc"),
            pl.lit(value=False).alias("_settled"),
        )
        notes.append("no tiered settlements table: tiered rebates remain accrual estimates")

    per = per.with_columns(
        pl.when(pl.col("_settled"))
        .then(pl.col("_actual_alloc"))
        .otherwise(pl.col("_accrued"))
        .alias("_used")
    )

    # Per-line aggregation across programs: sums, never row duplication.
    narrow = (
        per.group_by(row_col)
        .agg(
            pl.col("_accrued").sum().alias("tiered_accrued_cents"),
            pl.col("_actual_alloc").sum().alias("tiered_actual_alloc_cents"),
            pl.col("_used").sum().alias("tiered_rebate_used_cents"),
            pl.col("_rate_bp").sum().alias("tiered_rate_bp"),
            pl.col("_settled").all().alias("_all_settled"),
            pl.col("_settled").any().alias("_any_settled"),
        )
        .with_columns(
            pl.when(pl.col("_all_settled"))
            .then(pl.lit("actual"))
            .when(pl.col("_any_settled"))
            .then(pl.lit("partially_settled"))
            .otherwise(pl.lit("estimated"))
            .alias("tiered_rebate_status")
        )
        .sort(row_col)
        .select(row_col, *TIERED_COLS)
    )

    # True-up: actual minus accrued on SETTLED program-rows (unchanged meaning
    # for single-program customers).
    settled_rows = per.filter(pl.col("_settled"))
    residuals["tiered_true_up_cents"] = int(settled_rows["_actual_alloc"].sum()) - int(
        settled_rows["_accrued"].sum()
    )

    summary = (
        per.group_by("customer_id", "_year", "_pgm")
        .agg(
            pl.col("net_amount_cents").sum().alias("eligible_basis_cents"),
            pl.col("_accrued").sum().alias("accrued_cents"),
            pl.col("_settled").first().alias("settled"),
            pl.col("_actual_alloc").sum().alias("actual_alloc_cents"),
        )
        .with_columns(
            pl.when(pl.col("settled"))
            .then(pl.lit("actual"))
            .otherwise(pl.lit("estimated"))
            .alias("status"),
            (pl.col("eligible_basis_cents") < 0).alias("negative_basis"),
        )
        .rename({"_year": "year", "_pgm": "rebate_program_id"})
        .sort("customer_id", "year", "rebate_program_id")
        .select(list(TIERED_SUMMARY_SCHEMA))
        .cast(TIERED_SUMMARY_SCHEMA)  # type: ignore[arg-type]
    )
    return TieredResult(narrow=narrow, summary=summary)
