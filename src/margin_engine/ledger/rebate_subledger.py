"""Rebate lifecycle subledger (Run 6, Phase 62).

Rebates are a lifecycle, not a single number: estimate -> revisions ->
settlement -> reversal / true-up. The subledger holds one entry per lifecycle
step at monthly grain per customer and program, and the balance view enforces
the invariant

    opening accrual + new estimates + revisions - settlements - reversals
        = closing balance

for every period, with each settlement row applied exactly once (unique source
reference) and replacing the estimates it settles (the true-up revision closes
the gap, so a settled period's balance returns to zero).

Streams covered:

- Settlement-kind programs (a flat program rate settled quarterly): monthly
  estimates = program rate x the month's net basis; each settlement row is one
  settlement entry assigned to its quarter's closing month; the true-up
  revision books the difference between what was settled and what was
  estimated (a retroactively changed settlement simply arrives as another
  settlement row and the true-up absorbs it).
- Tiered programs: monthly estimates are the engine's per-line accruals; a
  RETROSPECTIVE top-up revision books rate(cum) x cum - accruals-to-date when
  a tier is crossed, because crossing a tier re-rates the whole cumulative
  basis, not just the marginal volume; the year's settlement replaces the
  estimates with the true-up revision closing the year.
- Clawbacks: a return that references an invoice from a PRIOR, already
  SETTLED program year books a reversal at that year's effective settled
  rate (estimated). A return referencing a prior UNSETTLED year books a
  negative revision against that year instead. Batch data carries no
  settlement timestamps, so "after settlement" means a later period than the
  settled one; when such a return also accrued in its own year, the overlap
  is noted — exact attribution needs transaction-time sequencing the source
  data does not carry.
- Line-accrued rebate programs are actual at invoice (no accrual lifecycle)
  and are stated as outside the subledger.

Every estimated amount is labeled estimated; settlements are actual.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from margin_engine.money import div_half_even_expr

ENTRY_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "customer_id": pl.String,
    "rebate_program_id": pl.String,
    "period": pl.String,  # YYYY-MM
    "entry_type": pl.String,  # estimate | revision | settlement | reversal
    "amount_cents": pl.Int64,  # positive raises the accrual; settlements and
    # reversals are stored positive and SUBTRACT in the balance identity
    "status": pl.String,  # estimated | actual
    "source_ref": pl.String,
    "link": pl.String,
    "note": pl.String,
}

BALANCES_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "customer_id": pl.String,
    "rebate_program_id": pl.String,
    "period": pl.String,
    "opening_cents": pl.Int64,
    "estimates_cents": pl.Int64,
    "revisions_cents": pl.Int64,
    "settlements_cents": pl.Int64,
    "reversals_cents": pl.Int64,
    "closing_cents": pl.Int64,
}


class SubledgerError(AssertionError):
    """The lifecycle invariant broke: a settlement applied twice, or balances
    that do not chain."""


@dataclass
class SubledgerResult:
    entries: pl.DataFrame
    balances: pl.DataFrame
    notes: list[str] = field(default_factory=list)


def _quarter_end_month(period: str) -> str:
    """'2024-Q1' -> '2024-03'. Unknown shapes pass through unchanged."""
    if "-Q" in period:
        year, q = period.split("-Q", 1)
        if q.isdigit():
            return f"{year}-{int(q) * 3:02d}"
    return period


def _entries_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=ENTRY_SCHEMA)
    return pl.DataFrame(rows, schema=ENTRY_SCHEMA)


def build_rebate_subledger(
    ledger: pl.DataFrame,
    rebate_programs: pl.DataFrame | None,
    rebate_settlements: pl.DataFrame | None,
    tiered_monthly: pl.DataFrame | None,
    tiered_summary: pl.DataFrame | None,
) -> SubledgerResult:
    notes: list[str] = []
    frames: list[pl.DataFrame] = []
    if rebate_programs is None:
        return SubledgerResult(
            entries=pl.DataFrame(schema=ENTRY_SCHEMA),
            balances=pl.DataFrame(schema=BALANCES_SCHEMA),
            notes=["no rebate programs: subledger empty"],
        )

    kinds = set(rebate_programs["program_kind"].unique().to_list())
    if "line_accrual" in kinds:
        notes.append(
            "line-accrued rebate programs are actual at invoice and carry no "
            "accrual lifecycle; they are outside the subledger by design"
        )
    unknown = kinds - {"line_accrual", "settlement", "tiered"}
    if unknown:
        notes.append(
            f"rebate program kinds {sorted(unknown)} have no lifecycle rule; "
            f"their programs are outside the subledger (stated, never guessed)"
        )

    base = ledger.filter(
        pl.col("transaction_type").is_in(["sale", "return"]) & (pl.col("net_amount_cents") != 0)
    ).select(
        pl.col("customer_id").cast(pl.String),
        pl.col("invoice_date"),
        pl.col("net_amount_cents"),
    )

    # --- settlement-kind programs: monthly estimates, quarterly settlements ---
    settle_prog = rebate_programs.filter(pl.col("program_kind") == "settlement").select(
        pl.col("customer_id").cast(pl.String),
        pl.col("rebate_program_id").cast(pl.String),
        (pl.col("rate_pct") * 100).round(0).cast(pl.Int64).alias("rate_bp"),
    )
    if settle_prog.height:
        monthly_net = (
            base.join(settle_prog, on="customer_id", how="inner")
            .with_columns(pl.col("invoice_date").dt.strftime("%Y-%m").alias("period"))
            .group_by("customer_id", "rebate_program_id", "rate_bp", "period")
            .agg(pl.col("net_amount_cents").sum().alias("basis_cents"))
            .with_columns(
                div_half_even_expr(pl.col("basis_cents") * pl.col("rate_bp"), 10_000).alias(
                    "amount_cents"
                )
            )
            .sort("customer_id", "rebate_program_id", "period")
        )
        est = monthly_net.select(
            "customer_id",
            "rebate_program_id",
            "period",
            pl.lit("estimate").alias("entry_type"),
            pl.col("amount_cents"),
            pl.lit("estimated").alias("status"),
            pl.concat_str(
                [
                    pl.lit("est|"),
                    pl.col("customer_id"),
                    pl.lit("|"),
                    pl.col("rebate_program_id"),
                    pl.lit("|"),
                    pl.col("period"),
                ]
            ).alias("source_ref"),
            pl.lit(None, dtype=pl.String).alias("link"),
            pl.lit("program rate x month net basis").alias("note"),
        )
        frames.append(est.select(list(ENTRY_SCHEMA)).cast(ENTRY_SCHEMA))  # type: ignore[arg-type]

        if rebate_settlements is not None and rebate_settlements.height:
            srows = rebate_settlements.with_columns(
                pl.int_range(pl.len(), dtype=pl.Int64).alias("_row"),
                pl.col("customer_id").cast(pl.String),
            )
            # Match each settlement row to ITS program when the table names
            # one; otherwise by customer, which is unambiguous only for
            # single-program customers (a multi-program row without a program
            # id is skipped with a note - never applied twice).
            if "rebate_program_id" in srows.columns:
                srows = srows.with_columns(pl.col("rebate_program_id").cast(pl.String)).join(
                    settle_prog.select("customer_id", "rebate_program_id").unique(),
                    on=["customer_id", "rebate_program_id"],
                    how="inner",
                )
            else:
                counts = settle_prog.group_by("customer_id").len()
                multi = counts.filter(pl.col("len") > 1)
                if multi.height:
                    notes.append(
                        "settlement rows carry no program id and at least one "
                        "customer holds several settlement programs: those rows "
                        "are outside the subledger (never applied twice)"
                    )
                srows = srows.join(
                    settle_prog.join(
                        counts.filter(pl.col("len") == 1).select("customer_id"),
                        on="customer_id",
                        how="semi",
                    ).select("customer_id", "rebate_program_id"),
                    on="customer_id",
                    how="inner",
                )
            srows = srows.with_columns(
                pl.col("period").cast(pl.String).alias("settle_period"),
                pl.col("period")
                .cast(pl.String)
                .map_elements(_quarter_end_month, return_dtype=pl.String)
                .alias("month"),
            )
            set_entries = srows.select(
                "customer_id",
                "rebate_program_id",
                pl.col("month").alias("period"),
                pl.lit("settlement").alias("entry_type"),
                pl.col("amount_cents"),
                pl.lit("actual").alias("status"),
                pl.concat_str(
                    [
                        pl.lit("settle|"),
                        pl.col("customer_id"),
                        pl.lit("|"),
                        pl.col("settle_period"),
                        pl.lit("#"),
                        pl.col("_row").cast(pl.String),
                    ]
                ).alias("source_ref"),
                pl.col("settle_period").alias("link"),
                pl.lit("settlement row; applied exactly once").alias("note"),
            )
            frames.append(set_entries.select(list(ENTRY_SCHEMA)).cast(ENTRY_SCHEMA))  # type: ignore[arg-type]

            # True-up per settled quarter: settled total minus the quarter's
            # estimates; the revision replaces the estimates so the quarter's
            # closing balance returns to zero.
            est_q = (
                monthly_net.with_columns(
                    pl.col("period")
                    .map_elements(_q_of_month, return_dtype=pl.String)
                    .alias("settle_period")
                )
                .group_by("customer_id", "rebate_program_id", "settle_period")
                .agg(pl.col("amount_cents").sum().alias("est_cents"))
            )
            set_q = srows.group_by(
                "customer_id", "rebate_program_id", "settle_period", "month"
            ).agg(pl.col("amount_cents").sum().alias("set_cents"))
            true_up = (
                set_q.join(
                    est_q, on=["customer_id", "rebate_program_id", "settle_period"], how="left"
                )
                .with_columns(pl.col("est_cents").fill_null(0))
                .with_columns((pl.col("set_cents") - pl.col("est_cents")).alias("amount_cents"))
                .filter(pl.col("amount_cents") != 0)
            )
            if true_up.height:
                tu = true_up.select(
                    "customer_id",
                    "rebate_program_id",
                    pl.col("month").alias("period"),
                    pl.lit("revision").alias("entry_type"),
                    pl.col("amount_cents"),
                    pl.lit("actual").alias("status"),
                    pl.concat_str(
                        [
                            pl.lit("trueup|"),
                            pl.col("customer_id"),
                            pl.lit("|"),
                            pl.col("rebate_program_id"),
                            pl.lit("|"),
                            pl.col("settle_period"),
                        ]
                    ).alias("source_ref"),
                    pl.col("settle_period").alias("link"),
                    pl.lit("settlement true-up: settled amount replaces the estimates").alias(
                        "note"
                    ),
                )
                frames.append(tu.select(list(ENTRY_SCHEMA)).cast(ENTRY_SCHEMA))  # type: ignore[arg-type]

    # --- tiered programs: estimates + retrospective revisions + settlement ----
    if tiered_monthly is not None and tiered_monthly.height:
        tm = tiered_monthly.sort("customer_id", "rebate_program_id", "year", "period")
        tm = tm.with_columns(
            div_half_even_expr(pl.col("cum_end_cents") * pl.col("rate_end_bp"), 10_000).alias(
                "_target"
            )
        ).with_columns(
            pl.col("accrued_cents")
            .cum_sum()
            .over("customer_id", "rebate_program_id", "year")
            .alias("_cum_acc")
        )
        tm = tm.with_columns((pl.col("_target") - pl.col("_cum_acc")).alias("_gap")).with_columns(
            (
                pl.col("_gap")
                - pl.col("_gap")
                .shift(1, fill_value=0)
                .over("customer_id", "rebate_program_id", "year")
            ).alias("_rev")
        )
        t_est = tm.filter(pl.col("accrued_cents") != 0).select(
            "customer_id",
            "rebate_program_id",
            "period",
            pl.lit("estimate").alias("entry_type"),
            pl.col("accrued_cents").alias("amount_cents"),
            pl.lit("estimated").alias("status"),
            pl.concat_str(
                [
                    pl.lit("tiermo|"),
                    pl.col("customer_id"),
                    pl.lit("|"),
                    pl.col("rebate_program_id"),
                    pl.lit("|"),
                    pl.col("period"),
                ]
            ).alias("source_ref"),
            pl.lit(None, dtype=pl.String).alias("link"),
            pl.lit("per-line tier accruals for the month").alias("note"),
        )
        frames.append(t_est.select(list(ENTRY_SCHEMA)).cast(ENTRY_SCHEMA))  # type: ignore[arg-type]
        t_rev = tm.filter(pl.col("_rev") != 0).select(
            "customer_id",
            "rebate_program_id",
            "period",
            pl.lit("revision").alias("entry_type"),
            pl.col("_rev").alias("amount_cents"),
            pl.lit("estimated").alias("status"),
            pl.concat_str(
                [
                    pl.lit("tierrev|"),
                    pl.col("customer_id"),
                    pl.lit("|"),
                    pl.col("rebate_program_id"),
                    pl.lit("|"),
                    pl.col("period"),
                ]
            ).alias("source_ref"),
            pl.lit(None, dtype=pl.String).alias("link"),
            pl.lit(
                "retrospective top-up: crossing a tier re-rates the whole "
                "cumulative basis, not just the marginal volume"
            ).alias("note"),
        )
        frames.append(t_rev.select(list(ENTRY_SCHEMA)).cast(ENTRY_SCHEMA))  # type: ignore[arg-type]

        if tiered_summary is not None and tiered_summary.height:
            last_month = tm.group_by("customer_id", "rebate_program_id", "year").agg(
                pl.col("period").max().alias("period")
            )
            settled = tiered_summary.filter(pl.col("settled")).join(
                last_month, on=["customer_id", "rebate_program_id", "year"], how="left"
            )
            if settled.height:
                t_set = settled.select(
                    "customer_id",
                    "rebate_program_id",
                    pl.col("period"),
                    pl.lit("settlement").alias("entry_type"),
                    pl.col("actual_alloc_cents").alias("amount_cents"),
                    pl.lit("actual").alias("status"),
                    pl.concat_str(
                        [
                            pl.lit("tierset|"),
                            pl.col("customer_id"),
                            pl.lit("|"),
                            pl.col("year"),
                            pl.lit("|"),
                            pl.col("rebate_program_id"),
                        ]
                    ).alias("source_ref"),
                    pl.col("year").alias("link"),
                    pl.lit("year settlement; applied exactly once").alias("note"),
                )
                frames.append(t_set.select(list(ENTRY_SCHEMA)).cast(ENTRY_SCHEMA))  # type: ignore[arg-type]
                # True-up: settled amount replaces the year's estimate stream.
                year_accrual = tm.group_by("customer_id", "rebate_program_id", "year").agg(
                    (pl.col("accrued_cents").sum() + pl.col("_rev").sum()).alias("acc")
                )
                t_tu = (
                    settled.join(
                        year_accrual, on=["customer_id", "rebate_program_id", "year"], how="left"
                    )
                    .with_columns(
                        (pl.col("actual_alloc_cents") - pl.col("acc").fill_null(0)).alias(
                            "amount_cents"
                        )
                    )
                    .filter(pl.col("amount_cents") != 0)
                )
                if t_tu.height:
                    tu2 = t_tu.select(
                        "customer_id",
                        "rebate_program_id",
                        pl.col("period"),
                        pl.lit("revision").alias("entry_type"),
                        pl.col("amount_cents"),
                        pl.lit("actual").alias("status"),
                        pl.concat_str(
                            [
                                pl.lit("tiertu|"),
                                pl.col("customer_id"),
                                pl.lit("|"),
                                pl.col("year"),
                                pl.lit("|"),
                                pl.col("rebate_program_id"),
                            ]
                        ).alias("source_ref"),
                        pl.col("year").alias("link"),
                        pl.lit("settlement true-up: settled amount replaces the estimates").alias(
                            "note"
                        ),
                    )
                    frames.append(tu2.select(list(ENTRY_SCHEMA)).cast(ENTRY_SCHEMA))  # type: ignore[arg-type]

        # Clawbacks: returns referencing an invoice from a PRIOR program year.
        claw_frames = _clawbacks(ledger, tiered_monthly, tiered_summary, notes)
        frames.extend(claw_frames)

    entries = (
        pl.concat([f for f in frames if f.height > 0], how="vertical")
        if any(f.height for f in frames)
        else pl.DataFrame(schema=ENTRY_SCHEMA)
    )
    entries = entries.sort(
        ["customer_id", "rebate_program_id", "period", "entry_type", "source_ref"]
    )
    balances = compute_balances(entries)
    validate_subledger(entries, balances)
    return SubledgerResult(entries=entries, balances=balances, notes=notes)


def _q_of_month(period: str) -> str:
    """'2024-03' -> '2024-Q1'."""
    year, _, month = period.partition("-")
    try:
        q = (int(month) + 2) // 3
    except ValueError:
        return period
    return f"{year}-Q{q}"


def _clawbacks(
    ledger: pl.DataFrame,
    tiered_monthly: pl.DataFrame,
    tiered_summary: pl.DataFrame | None,
    notes: list[str],
) -> list[pl.DataFrame]:
    if "original_invoice_ref" not in ledger.columns:
        return []
    rets = ledger.filter(
        (pl.col("transaction_type") == "return") & pl.col("original_invoice_ref").is_not_null()
    ).select(
        pl.col("customer_id").cast(pl.String),
        pl.col("original_invoice_ref").cast(pl.String),
        pl.col("invoice_date"),
        pl.col("net_amount_cents"),
        pl.col("source_line_key").cast(pl.String),
    )
    if rets.height == 0:
        return []
    orig_year = (
        ledger.filter(pl.col("transaction_type") == "sale")
        .group_by(pl.col("invoice_id").cast(pl.String).alias("original_invoice_ref"))
        .agg(pl.col("invoice_date").dt.year().cast(pl.String).min().alias("orig_year"))
    )
    rets = (
        rets.join(orig_year, on="original_invoice_ref", how="inner")
        .with_columns(
            pl.col("invoice_date").dt.year().cast(pl.String).alias("ret_year"),
            pl.col("invoice_date").dt.strftime("%Y-%m").alias("period"),
        )
        .filter(pl.col("ret_year") > pl.col("orig_year"))
    )
    if rets.height == 0:
        return []
    # Effective rate of the referenced year: settled years use the settled
    # effective rate; unsettled years use the tier rate at year end.
    year_rate = (
        tiered_monthly.sort("period")
        .group_by("customer_id", "rebate_program_id", "year", maintain_order=True)
        .agg(pl.col("rate_end_bp").last().alias("rate_end_bp"))
    )
    if tiered_summary is not None and tiered_summary.height:
        eff = tiered_summary.join(
            year_rate, on=["customer_id", "rebate_program_id", "year"], how="left"
        ).with_columns(
            pl.when(
                pl.col("settled")
                & (pl.col("eligible_basis_cents") > 0)
                & (pl.col("actual_alloc_cents") != 0)
            )
            .then(
                (pl.col("actual_alloc_cents") * 10_000 / pl.col("eligible_basis_cents"))
                .round(0)
                .cast(pl.Int64)
            )
            .otherwise(pl.col("rate_end_bp"))
            .alias("eff_bp"),
        )
    else:
        eff = year_rate.with_columns(
            pl.col("rate_end_bp").alias("eff_bp"), pl.lit(value=False).alias("settled")
        )
    claws = (
        rets.join(
            eff.select(
                "customer_id",
                "rebate_program_id",
                pl.col("year").alias("orig_year"),
                "eff_bp",
                "settled",
            ),
            on=["customer_id", "orig_year"],
            how="inner",
        )
        .with_columns(
            div_half_even_expr(-pl.col("net_amount_cents") * pl.col("eff_bp"), 10_000).alias(
                "claw_cents"
            )
        )
        .filter(pl.col("claw_cents") != 0)
    )
    if claws.height == 0:
        return []
    notes.append(
        f"{claws.height} return(s) reference an invoice from a prior rebate "
        "year: clawback entries booked at that year's effective rate "
        "(estimated; batch data carries no settlement timestamps, so 'after "
        "settlement' means a later period than the settled one)"
    )
    out = claws.select(
        "customer_id",
        "rebate_program_id",
        "period",
        pl.when(pl.col("settled"))
        .then(pl.lit("reversal"))
        .otherwise(pl.lit("revision"))
        .alias("entry_type"),
        # A reversal SUBTRACTS in the balance identity, so the clawback is
        # stored positive there; the unsettled case lowers the still-open
        # accrual estimate, a negative revision.
        pl.when(pl.col("settled"))
        .then(pl.col("claw_cents"))
        .otherwise(-pl.col("claw_cents"))
        .alias("amount_cents"),
        pl.lit("estimated").alias("status"),
        pl.concat_str([pl.lit("claw|"), pl.col("source_line_key")]).alias("source_ref"),
        pl.concat_str([pl.col("customer_id"), pl.lit("|"), pl.col("orig_year")]).alias("link"),
        pl.when(pl.col("settled"))
        .then(pl.lit("clawback: return against a settled prior year"))
        .otherwise(pl.lit("return against a prior unsettled year lowers its accrual"))
        .alias("note"),
    )
    return [out.select(list(ENTRY_SCHEMA)).cast(ENTRY_SCHEMA)]  # type: ignore[arg-type]


def compute_balances(entries: pl.DataFrame) -> pl.DataFrame:
    """Per customer x program x period: the lifecycle identity columns and the
    running closing balance."""
    if entries.height == 0:
        return pl.DataFrame(schema=BALANCES_SCHEMA)
    per = (
        entries.group_by("customer_id", "rebate_program_id", "period")
        .agg(
            pl.col("amount_cents")
            .filter(pl.col("entry_type") == "estimate")
            .sum()
            .fill_null(0)
            .alias("estimates_cents"),
            pl.col("amount_cents")
            .filter(pl.col("entry_type") == "revision")
            .sum()
            .fill_null(0)
            .alias("revisions_cents"),
            pl.col("amount_cents")
            .filter(pl.col("entry_type") == "settlement")
            .sum()
            .fill_null(0)
            .alias("settlements_cents"),
            pl.col("amount_cents")
            .filter(pl.col("entry_type") == "reversal")
            .sum()
            .fill_null(0)
            .alias("reversals_cents"),
        )
        .sort("customer_id", "rebate_program_id", "period")
    )
    per = per.with_columns(
        (
            pl.col("estimates_cents")
            + pl.col("revisions_cents")
            - pl.col("settlements_cents")
            - pl.col("reversals_cents")
        )
        .cum_sum()
        .over("customer_id", "rebate_program_id")
        .alias("closing_cents")
    ).with_columns(
        (
            pl.col("closing_cents")
            - pl.col("estimates_cents")
            - pl.col("revisions_cents")
            + pl.col("settlements_cents")
            + pl.col("reversals_cents")
        ).alias("opening_cents")
    )
    return per.select(list(BALANCES_SCHEMA)).cast(BALANCES_SCHEMA)  # type: ignore[arg-type]


def validate_subledger(entries: pl.DataFrame, balances: pl.DataFrame) -> None:
    """Enforce the lifecycle invariant. Raises SubledgerError on violation."""
    if entries.height == 0:
        return
    # Each settlement applied exactly once: settlement source refs are unique.
    setts = entries.filter(pl.col("entry_type") == "settlement")
    dup = setts.group_by("source_ref").len().filter(pl.col("len") > 1)
    if dup.height:
        refs = dup["source_ref"].head(5).to_list()
        msg = f"settlement source rows applied more than once: {refs}"
        raise SubledgerError(msg)
    # Balance identity per period, and opening chains to the prior closing.
    bad = balances.filter(
        pl.col("closing_cents")
        != pl.col("opening_cents")
        + pl.col("estimates_cents")
        + pl.col("revisions_cents")
        - pl.col("settlements_cents")
        - pl.col("reversals_cents")
    )
    if bad.height:
        msg = f"{bad.height} period(s) break the lifecycle balance identity"
        raise SubledgerError(msg)
    chain = balances.sort("customer_id", "rebate_program_id", "period").with_columns(
        pl.col("closing_cents")
        .shift(1, fill_value=0)
        .over("customer_id", "rebate_program_id")
        .alias("_prev_closing")
    )
    broken = chain.filter(pl.col("opening_cents") != pl.col("_prev_closing"))
    if broken.height:
        msg = f"{broken.height} period(s) do not chain opening to prior closing"
        raise SubledgerError(msg)
    # Unknown entry types never enter silently.
    known = {"estimate", "revision", "settlement", "reversal"}
    stray = set(entries["entry_type"].unique().to_list()) - known
    if stray:
        msg = f"unknown subledger entry types: {sorted(stray)}"
        raise SubledgerError(msg)
