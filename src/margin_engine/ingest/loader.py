"""Load a source export through its mapping into the canonical schema.

All columns are read as strings; money parses digit-exact into Int64 cents.
The result is a canonical Polars frame whose column names and semantics no
longer depend on the source system: equivalent data through different mappings
produces identical canonical frames.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from margin_engine.ingest.mapping import MappingError, SourceMapping
from margin_engine.money import (
    assert_parse_ok,
    parse_money_expr,
    scaled_to_cents,
)

CANONICAL_MONEY_FIELDS = {
    # canonical field -> cents column name
    "net_amount": "net_amount_cents",
    "on_invoice_discount": "discount_cents",
    "customer_rebate_amount": "rebate_cents",
    "freight_concession": "freight_concession_cents",
}


def _require_columns(df: pl.DataFrame, cols: list[str], file_label: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        msg = (
            f"{file_label}: mapped column(s) {missing} not found in the file. "
            f"Available columns: {df.columns}. Fix the mapping or the export."
        )
        raise MappingError(msg)


def load_lines(data_dir: Path, mapping: SourceMapping) -> pl.DataFrame:
    """Load invoice lines into the canonical schema (money in Int64 cents)."""
    path = data_dir / mapping.lines_file
    if not path.exists():
        msg = f"lines file not found: {path}"
        raise MappingError(msg)
    raw = pl.read_csv(path, infer_schema=False)
    lm = mapping.lines
    sep = mapping.decimal_separator

    declared: list[str] = []
    for f in type(lm).model_fields:
        spec = getattr(lm, f)
        if spec is not None:
            declared.append(spec.column)
    _require_columns(raw, declared, str(path))

    out = raw.select(
        pl.col(lm.source_line_key.column).alias("source_line_key"),
        pl.col(lm.invoice_id.column).alias("invoice_id"),
        pl.col(lm.invoice_date.column)
        .str.to_date(lm.invoice_date.format, strict=False)
        .alias("invoice_date"),
        pl.col(lm.customer_id.column).alias("customer_id"),
        pl.col(lm.item_id.column).alias("item_id"),
        pl.col(lm.quantity.column).cast(pl.Float64, strict=False).alias("quantity"),
    )
    # Unparseable dates are a data error, reported actionably with line keys.
    bad_dates = out.filter(pl.col("invoice_date").is_null())
    if bad_dates.height:
        keys = bad_dates["source_line_key"].head(10).to_list()
        msg = (
            f"{path}: {bad_dates.height} invoice_date value(s) do not match declared "
            f"format '{lm.invoice_date.format}'. Offending line keys (up to 10): {keys}"
        )
        raise MappingError(msg)

    extra: list[pl.Expr] = []
    if lm.uom is not None:
        extra.append(pl.col(lm.uom.column).alias("uom"))
    if lm.order_id is not None:
        extra.append(pl.col(lm.order_id.column).alias("order_id"))
    for key_field in (
        "credit_reason",
        "original_invoice_ref",
        "sales_rep_id",
        "channel",
        "rebate_program_id",
        "contract_id",
        "currency",
    ):
        spec = getattr(lm, key_field)
        if spec is not None:
            extra.append(pl.col(spec.column).replace("", None).alias(key_field))
    if lm.transaction_type is not None:
        extra.append(pl.col(lm.transaction_type.column).alias("_txn_raw"))
    out = pl.concat([out, raw.select(extra)], how="horizontal")

    # --- money fields, digit-exact ---------------------------------------
    money_raw: list[tuple[str, str, str]] = []  # (canonical, source col, cents col)
    if lm.net_amount is not None:
        money_raw.append(("net_amount", lm.net_amount.column, "net_amount_cents"))
    if lm.on_invoice_discount is not None:
        money_raw.append(("on_invoice_discount", lm.on_invoice_discount.column, "discount_cents"))
    if lm.customer_rebate_amount is not None:
        money_raw.append(
            ("customer_rebate_amount", lm.customer_rebate_amount.column, "rebate_cents")
        )
    if lm.freight_concession is not None:
        money_raw.append(
            ("freight_concession", lm.freight_concession.column, "freight_concession_cents")
        )

    scaled_cols = {}
    for _canon, src, cents in money_raw:
        scaled_name = f"_{cents}_scaled"
        scaled_cols[scaled_name] = (src, cents)
    parsed = raw.select(
        [parse_money_expr(src, sep).alias(name) for name, (src, _) in scaled_cols.items()]
    )
    check = pl.concat([raw, parsed], how="horizontal")
    for name, (src, _) in scaled_cols.items():
        assert_parse_ok(check, src, name, str(path), sep, lm.source_line_key.column)
    out = pl.concat(
        [
            out,
            parsed.select(
                [
                    scaled_to_cents(pl.col(name)).alias(cents)
                    for name, (_, cents) in scaled_cols.items()
                ]
            ),
        ],
        how="horizontal",
    )

    # Unit price and list price: per-unit values at parse scale (ten-thousandths).
    if lm.unit_price is not None:
        up = raw.select(parse_money_expr(lm.unit_price.column, sep).alias("unit_price_scaled"))
        assert_parse_ok(
            pl.concat([raw, up], how="horizontal"),
            lm.unit_price.column,
            "unit_price_scaled",
            str(path),
            sep,
            lm.source_line_key.column,
        )
        out = pl.concat([out, up], how="horizontal")
        if lm.net_amount is None and lm.unit_price.authoritative:
            # net = quantity x unit price, half-even to cents, integer math.
            qty_scaled = raw.select(parse_money_expr(lm.quantity.column, sep).alias("_q"))["_q"]
            out = (
                out.with_columns(pl.Series("_q_scaled", qty_scaled))
                .with_columns(
                    scaled_to_cents(
                        (pl.col("_q_scaled") * pl.col("unit_price_scaled")).floordiv(10_000)
                    ).alias("net_amount_cents")
                )
                .drop("_q_scaled")
            )
    if lm.list_price is not None:
        lp = raw.select(parse_money_expr(lm.list_price.column, sep).alias("list_price_scaled"))
        assert_parse_ok(
            pl.concat([raw, lp], how="horizontal"),
            lm.list_price.column,
            "list_price_scaled",
            str(path),
            sep,
            lm.source_line_key.column,
        )
        out = pl.concat([out, lp], how="horizontal")

    if lm.unit_cost is not None:
        uc = raw.select(parse_money_expr(lm.unit_cost.column, sep).alias("unit_cost_scaled"))
        assert_parse_ok(
            pl.concat([raw, uc], how="horizontal"),
            lm.unit_cost.column,
            "unit_cost_scaled",
            str(path),
            sep,
            lm.source_line_key.column,
        )
        out = pl.concat([out, uc], how="horizontal")
        if lm.unit_cost.missing_as == "zero_is_missing":
            out = out.with_columns(
                pl.when(pl.col("unit_cost_scaled") == 0)
                .then(None)
                .otherwise(pl.col("unit_cost_scaled"))
                .alias("unit_cost_scaled")
            )
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Int64).alias("unit_cost_scaled"))

    # --- transaction type --------------------------------------------------
    if lm.transaction_type is not None:
        code_map = lm.transaction_type.code_map
        unknown = out.filter(
            ~pl.col("_txn_raw").is_in(list(code_map.keys())) & pl.col("_txn_raw").is_not_null()
        )
        if unknown.height:
            codes = unknown["_txn_raw"].unique().head(10).to_list()
            msg = (
                f"{path}: transaction codes {codes} not declared in the mapping's "
                f"code_map {code_map}. Declare every code; the engine never guesses."
            )
            raise MappingError(msg)
        out = out.with_columns(
            pl.col("_txn_raw").replace_strict(code_map, default=None).alias("transaction_type")
        ).drop("_txn_raw")
    else:
        msg = (
            f"mapping '{mapping.source_system}' declares no transaction_type and no "
            f"derivation rule; cannot classify sales vs returns vs credits. "
            f"Add a transaction_type entry with a code_map."
        )
        raise MappingError(msg)

    # Normalize credit reasons through the declared map (unmapped reasons kept raw).
    if mapping.credit_reason_map and "credit_reason" in out.columns:
        out = out.with_columns(
            pl.col("credit_reason").replace(mapping.credit_reason_map).alias("credit_reason")
        )
    return out


def load_reference_table(data_dir: Path, mapping: SourceMapping, name: str) -> pl.DataFrame | None:
    """Load one reference table into canonical column names, or None if absent."""
    spec = mapping.reference_tables.get(name)
    if spec is None:
        return None
    path = data_dir / spec.file
    if not path.exists():
        return None
    raw = pl.read_csv(path, infer_schema=False)
    _require_columns(raw, list(spec.columns.values()), str(path))
    df = raw.select([pl.col(src).alias(canon) for canon, src in spec.columns.items()])
    # Typed columns by canonical name convention.
    money_cols = {
        "list_price": "list_price_scaled",
        "amount": "amount_cents",
        "inbound_freight_per_unit": "inbound_freight_pu_scaled",
        "duty_per_unit": "duty_pu_scaled",
    }
    exprs: list[pl.Expr] = []
    for canon in spec.columns:
        if canon in money_cols:
            target = money_cols[canon]
            e = parse_money_expr(canon, mapping.decimal_separator)
            exprs.append(
                scaled_to_cents(e).alias(target) if target.endswith("_cents") else e.alias(target)
            )
        elif canon in ("effective_date", "start_date", "end_date"):
            fmt = spec.date_format
            if fmt is None:
                msg = (
                    f"reference table '{name}' maps date column '{canon}' but declares "
                    f"no date_format."
                )
                raise MappingError(msg)
            exprs.append(pl.col(canon).str.to_date(fmt).alias(canon))
        elif canon in (
            "factor",
            "rate_pct",
            "discount_pct",
            "weight",
            "cube",
            "cost_idx",
            "duty_pct",
        ):
            exprs.append(pl.col(canon).cast(pl.Float64).alias(canon))
        elif canon in ("is_service", "price_protected"):
            exprs.append((pl.col(canon).cast(pl.Int64) != 0).alias(canon))
    df = df.with_columns(exprs)
    drop = [c for c in ("list_price",) if f"{c}_scaled" in df.columns and c in df.columns]
    return df.drop(drop) if drop else df
