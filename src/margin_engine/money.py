"""Exact money handling for the engine.

Policy (spec section 8.2): monetary amounts are exact — integer cents in all
internal Polars frames (Int64 columns suffixed `_cents`), Decimal at report
boundaries. Floats never carry a value that must reconcile.

String parsing is digit-wise (no float pass): "12.345" -> 123450 at scale 4.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

import polars as pl

PARSE_SCALE = 4  # ten-thousandths; covers 4dp unit prices and costs exactly
_SCALE_FACTOR = 10**PARSE_SCALE


class MoneyParseError(ValueError):
    """A money column contained a value that cannot be parsed exactly."""


def normalize_money_expr(
    expr: pl.Expr,
    decimal_separator: str = ".",
    currency_symbols: list[str] | None = None,
    negative_format: str = "minus",
) -> pl.Expr:
    """Normalize raw money strings to canonical '-123.45' form, digit-exact:
    trims, strips declared currency symbols, converts declared parentheses
    negatives to a leading minus, and resolves grouping vs decimal separators.
    Shared by the parser and its validator so both see identical values."""
    s = expr.cast(pl.String).str.strip_chars()
    for sym in currency_symbols or []:
        s = s.str.replace_all(sym, "", literal=True).str.strip_chars()
    if negative_format == "parentheses":
        wrapped = s.str.starts_with("(") & s.str.ends_with(")")
        s = (
            pl.when(wrapped)
            .then(pl.lit("-") + s.str.strip_chars("()").str.strip_chars())
            .otherwise(s)
        )
    if decimal_separator == ",":
        s = s.str.replace_all(".", "", literal=True).str.replace_all(",", ".", literal=True)
    else:
        s = s.str.replace_all(",", "", literal=True)
    return s


def parse_money_expr(
    col: str,
    decimal_separator: str = ".",
    currency_symbols: list[str] | None = None,
    negative_format: str = "minus",
) -> pl.Expr:
    """Parse a string money column into scaled Int64 (10^-4 units), digit-exact.

    Empty strings and nulls stay null. Raises downstream via `validate_parsed`
    when values have too many decimals or stray characters.
    """
    s = normalize_money_expr(pl.col(col), decimal_separator, currency_symbols, negative_format)
    s = s.replace("", None)
    neg = s.str.starts_with("-")
    body = s.str.strip_chars_start("+-")
    parts = body.str.split_exact(".", 1)
    whole = parts.struct.field("field_0").replace("", "0").cast(pl.Int64, strict=False)
    frac_str = parts.struct.field("field_1").fill_null("")
    frac = (frac_str.str.pad_end(PARSE_SCALE, "0").str.slice(0, PARSE_SCALE).replace("", "0")).cast(
        pl.Int64, strict=False
    )
    magnitude = whole * _SCALE_FACTOR + frac
    signed = pl.when(neg).then(-magnitude).otherwise(magnitude)
    # Preserve nulls; flag unparseable (non-null input but null parse) separately.
    return pl.when(s.is_null()).then(None).otherwise(signed)


def assert_parse_ok(
    df: pl.DataFrame,
    raw_col: str,
    parsed_col: str,
    file_label: str,
    decimal_separator: str = ".",
    key_col: str | None = None,
    currency_symbols: list[str] | None = None,
    negative_format: str = "minus",
) -> None:
    """Fail actionably when non-empty raw values did not parse. When key_col is
    given, up to 10 offending source line keys are listed."""

    def keys_of(frame: pl.DataFrame) -> str:
        if key_col and key_col in frame.columns:
            return f" Offending line keys (up to 10): {frame[key_col].head(10).to_list()}."
        return ""

    bad = df.filter(
        pl.col(raw_col).cast(pl.String).str.strip_chars().fill_null("").ne("")
        & pl.col(parsed_col).is_null()
    )
    if bad.height:
        sample = bad[raw_col].head(5).to_list()
        msg = (
            f"{file_label}: column '{raw_col}' has {bad.height} unparseable money "
            f"value(s), e.g. {sample}. Likely causes: a currency symbol not listed "
            f"in the mapping's input.currency_symbols, parentheses negatives without "
            f"input.negative_format: parentheses, or the wrong decimal_separator."
            f"{keys_of(bad)}"
        )
        raise MoneyParseError(msg)
    # Values with more than PARSE_SCALE decimals would be silently truncated: refuse.
    normalized = normalize_money_expr(
        pl.col(raw_col), decimal_separator, currency_symbols, negative_format
    )
    too_precise = df.filter(
        normalized.str.split_exact(".", 1)
        .struct.field("field_1")
        .fill_null("")
        .str.len_chars()
        .gt(PARSE_SCALE)
    )
    if too_precise.height:
        msg = (
            f"{file_label}: column '{raw_col}' has values with more than "
            f"{PARSE_SCALE} decimal places; refusing to truncate money.{keys_of(too_precise)}"
        )
        raise MoneyParseError(msg)


def div_half_even_expr(numer: pl.Expr, denom: int | pl.Expr) -> pl.Expr:
    """numer / denom rounded half-even, exact integer arithmetic (Int64 exprs).

    denom may be a per-row expression (must be positive at runtime)."""
    if isinstance(denom, int) and denom <= 0:
        msg = "denominator must be positive"
        raise ValueError(msg)
    q = numer.floordiv(denom)
    r = numer - q * denom  # 0 <= r < denom for any sign (polars floordiv floors)
    round_up = (2 * r > denom) | ((2 * r == denom) & (q % 2 != 0))
    return q + round_up.cast(pl.Int64)


def scaled_to_cents(expr: pl.Expr) -> pl.Expr:
    """Convert PARSE_SCALE ints to cents, half-even (exact for <=2dp data)."""
    return div_half_even_expr(expr, _SCALE_FACTOR // 100)


def cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def cents_col_to_dollars(col: str) -> pl.Expr:
    """Display-only conversion for exports; never used in reconciling math."""
    return (pl.col(col).cast(pl.Float64) / 100).round(2)
