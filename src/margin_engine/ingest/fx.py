"""Multi-currency conversion (Phase 9).

Policy: list prices and costs are declared in the reporting currency; revenue
amounts on foreign-customer lines arrive in the line's currency (the mapping
declares `currency: line_currency` on those fields) and are converted to the
reporting currency at ingest, at the rate in force for the invoice month
(`fx_rate_policy: invoice_date`, the default). Settlement-period amounts use
the rate of the period's last month. Conversion is exact integer arithmetic:
converted = half_even(cents x rate_ppm / 1e6), with the original amounts kept
in *_src columns and the applied rate on every line for audit.

A line currency with no rate for its period is an actionable error — the
engine never guesses an exchange rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from margin_engine.config import Rules
from margin_engine.ingest.mapping import MappingError, SourceMapping
from margin_engine.money import div_half_even_expr

# Engine columns converted when their mapping field declares line_currency.
FIELD_TO_COLUMNS: dict[str, list[str]] = {
    "net_amount": ["net_amount_cents"],
    "on_invoice_discount": ["discount_cents"],
    "customer_rebate_amount": ["rebate_cents"],
    "freight_concession": ["freight_concession_cents"],
    "restocking_fee": ["restocking_fee_cents"],
    "return_freight_billed": ["return_freight_billed_cents"],
    "unit_price": ["unit_price_scaled"],
    "list_price": ["list_price_scaled"],
}


@dataclass
class FxResult:
    lines: pl.DataFrame
    source_totals_by_currency: dict[str, int] = field(default_factory=dict)
    converted: bool = False
    notes: list[str] = field(default_factory=list)


def _rate_table(fx_rates: pl.DataFrame) -> pl.DataFrame:
    return fx_rates.with_columns(
        (pl.col("rate") * 1_000_000).round(0).cast(pl.Int64).alias("rate_ppm")
    ).select("currency", "period", "rate_ppm")


def convert_lines(
    lines: pl.DataFrame,
    mapping: SourceMapping,
    fx_rates: pl.DataFrame | None,
    rules: Rules,
) -> FxResult:
    reporting = rules.units_and_currency.reporting_currency
    if "currency" not in lines.columns:
        return FxResult(lines=lines)
    lines = lines.with_columns(pl.col("currency").fill_null(reporting))
    totals = {
        str(cur): int(total)
        for cur, total in lines.group_by("currency")
        .agg(pl.col("net_amount_cents").fill_null(0).sum())
        .iter_rows()
    }
    foreign = lines.filter(pl.col("currency") != reporting)
    if foreign.height == 0:
        return FxResult(lines=lines, source_totals_by_currency=totals)
    if fx_rates is None:
        msg = (
            f"lines are invoiced in {sorted(set(foreign['currency'].to_list()))} but no "
            f"fx_rates reference table is mapped; cannot convert to {reporting}. "
            f"Add an fx_rates table (currency, period, rate) to the mapping."
        )
        raise MappingError(msg)

    rates = _rate_table(fx_rates)
    policy = rules.units_and_currency.fx_rate_policy
    if policy != "invoice_date":
        msg = f"fx_rate_policy '{policy}' is not implemented; use invoice_date"
        raise MappingError(msg)
    lines = lines.with_columns(
        pl.col("invoice_date").dt.strftime("%Y-%m").alias("_fx_period")
    ).join(
        rates,
        left_on=["currency", "_fx_period"],
        right_on=["currency", "period"],
        how="left",
    )
    missing = lines.filter((pl.col("currency") != reporting) & pl.col("rate_ppm").is_null())
    if missing.height:
        pairs = missing.select("currency", "_fx_period").unique().head(10).to_dicts()
        msg = (
            f"no fx rate for {missing.height} line(s); missing currency-period "
            f"pairs (up to 10): {pairs}. The engine never guesses exchange rates."
        )
        raise MappingError(msg)
    lines = lines.with_columns(pl.col("rate_ppm").fill_null(1_000_000).alias("fx_rate_ppm"))

    is_foreign = pl.col("currency") != reporting
    exprs: list[pl.Expr] = []
    converted_cols: list[str] = []
    for fld, cols in FIELD_TO_COLUMNS.items():
        spec = getattr(mapping.lines, fld, None)
        if spec is None or getattr(spec, "currency", None) != "line_currency":
            continue
        for col in cols:
            if col not in lines.columns:
                continue
            converted_cols.append(col)
            exprs.append(pl.col(col).alias(f"{col}_src"))
            exprs.append(
                pl.when(is_foreign & pl.col(col).is_not_null())
                .then(div_half_even_expr(pl.col(col) * pl.col("fx_rate_ppm"), 1_000_000))
                .otherwise(pl.col(col))
                .alias(col)
            )
    lines = lines.with_columns(exprs).drop("_fx_period", "rate_ppm")
    return FxResult(
        lines=lines,
        source_totals_by_currency=totals,
        converted=True,
        notes=[
            f"converted {foreign.height} foreign-currency lines to {reporting} at "
            f"invoice-month rates (columns: {sorted(set(converted_cols))})"
        ],
    )


def convert_reference_amounts(
    table: pl.DataFrame,
    currency_of: pl.DataFrame,
    key: str,
    fx_rates: pl.DataFrame,
    reporting: str,
    *,
    period_col: str | None = None,
    period_rate: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Convert a reference table's amount_cents to the reporting currency.

    currency_of: frame mapping `key` -> currency (derived from the lines).
    When period_col is given the rate of that period is used (for quarters,
    callers pass a pre-resolved period_rate frame keyed by (currency, period)).
    """
    t = table.join(currency_of, on=key, how="left").with_columns(
        pl.col("currency").fill_null(reporting)
    )
    if t.filter(pl.col("currency") != reporting).height == 0:
        return t.drop("currency")
    rates = period_rate if period_rate is not None else _rate_table(fx_rates)
    join_period = period_col if period_col is not None else "period"
    t = t.join(
        rates.rename({"period": join_period}) if join_period != "period" else rates,
        left_on=["currency", join_period],
        right_on=["currency", join_period],
        how="left",
    )
    missing = t.filter((pl.col("currency") != reporting) & pl.col("rate_ppm").is_null())
    if missing.height:
        msg = (
            f"no fx rate for {missing.height} reference row(s): "
            f"{missing.select('currency', join_period).unique().head(10).to_dicts()}"
        )
        raise MappingError(msg)
    t = t.with_columns(pl.col("rate_ppm").fill_null(1_000_000))
    return t.with_columns(
        pl.col("amount_cents").alias("amount_src_cents"),
        pl.when(pl.col("currency") != reporting)
        .then(div_half_even_expr(pl.col("amount_cents") * pl.col("rate_ppm"), 1_000_000))
        .otherwise(pl.col("amount_cents"))
        .alias("amount_cents"),
    ).drop("rate_ppm")


def quarter_end_rates(fx_rates: pl.DataFrame) -> pl.DataFrame:
    """Quarterly rate table: the rate of each quarter's last available month."""
    rates = _rate_table(fx_rates)
    return (
        rates.with_columns(
            (
                pl.col("period").str.slice(0, 4)
                + "-Q"
                + ((pl.col("period").str.slice(5, 2).cast(pl.Int32) + 2) // 3).cast(pl.String)
            ).alias("quarter")
        )
        .sort("currency", "period")
        .group_by("currency", "quarter", maintain_order=True)
        .agg(pl.col("rate_ppm").last())
        .rename({"quarter": "period"})
    )
