"""Load a source export through its mapping into the canonical schema.

All columns are read as strings; money parses digit-exact into Int64 cents.
The result is a canonical Polars frame whose column names and semantics no
longer depend on the source system: equivalent data through different mappings
produces identical canonical frames.
"""

from __future__ import annotations

import io
from pathlib import Path

import polars as pl

from margin_engine.ingest.mapping import InputOptions, MappingError, SourceMapping
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


_DELIM_NAMES = {",": "','", ";": "';'", "	": "tab", "|": "'|'"}


def _excel_to_frame(path: Path, sheet: str | int | None, opts: InputOptions) -> pl.DataFrame:
    """Read one .xlsx sheet into an all-string frame via openpyxl (pure
    Python). Numbers render digit-preserving (trailing .0 trimmed); date cells
    render ISO (%Y-%m-%d) - declare that format in the mapping. The result
    joins the exact same validation path as CSV."""
    import datetime as dt

    from openpyxl import load_workbook

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        msg = (
            f"{path}: not a readable .xlsx workbook ({exc}). If this is a CSV "
            f"with an .xlsx name, rename it and map it as CSV."
        )
        raise MappingError(msg) from exc
    try:
        if sheet is None:
            ws = wb.worksheets[0]
        elif isinstance(sheet, int):
            if sheet >= len(wb.worksheets):
                msg = (
                    f"{path}: sheet index {sheet} out of range "
                    f"({len(wb.worksheets)} sheet(s)). Fix 'sheet' in the mapping."
                )
                raise MappingError(msg)
            ws = wb.worksheets[sheet]
        else:
            if sheet not in wb.sheetnames:
                msg = (
                    f"{path}: no sheet named '{sheet}'. Available: {wb.sheetnames}. "
                    f"Fix 'sheet' in the mapping."
                )
                raise MappingError(msg)
            ws = wb[sheet]

        def cell_str(v: object) -> str | None:
            if v is None:
                return None
            if isinstance(v, bool):
                return str(v)
            if isinstance(v, dt.datetime):
                return v.date().isoformat()
            if isinstance(v, dt.date):
                return v.isoformat()
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            return str(v)

        rows = ws.iter_rows(values_only=True)
        try:
            header = [str(h) if h is not None else "" for h in next(rows)]
        except StopIteration:
            msg = f"{path}: sheet '{ws.title}' is empty."
            raise MappingError(msg) from None
        data: dict[str, list[str | None]] = {h: [] for h in header}
        for row in rows:
            for h, v in zip(header, row, strict=False):
                data[h].append(cell_str(v))
        return pl.DataFrame(data, schema=dict.fromkeys(header, pl.String))
    finally:
        wb.close()


def read_table(
    path: Path, opts: InputOptions, sheet: str | int | None = None
) -> tuple[pl.DataFrame, int]:
    """Read a source table (CSV or .xlsx) into an all-string frame under the
    mapping's declared input options, returning (frame, n_blank_rows_dropped).
    Fully blank rows - every cell empty after trimming, including
    whitespace-only and trailing blank lines - are dropped and counted; they
    are not data. Any parse failure is reported with its likely cause and the
    mapping option to set - never a raw library error."""
    if path.suffix.lower() == ".xlsx":
        raw = _excel_to_frame(path, sheet, opts)
    else:
        blob = path.read_bytes()
        codec = (
            "utf-8-sig"
            if opts.encoding.lower().replace("_", "-") in ("utf-8", "utf8")
            else opts.encoding
        )
        try:
            text = blob.decode(codec)
        except (UnicodeDecodeError, LookupError) as exc:
            msg = (
                f"{path}: cannot decode as '{opts.encoding}' ({exc}). The file is "
                f"probably in another encoding (Windows exports are often "
                f"windows-1252); set input.encoding in the mapping."
            )
            raise MappingError(msg) from exc
        try:
            raw = pl.read_csv(
                io.BytesIO(text.encode("utf-8")),
                infer_schema=False,
                separator=opts.delimiter,
                quote_char='"',
            )
        except Exception as exc:
            msg = (
                f"{path}: could not be parsed as {_DELIM_NAMES[opts.delimiter]}-"
                f"delimited CSV ({exc}). Check input.delimiter and input.encoding "
                f"in the mapping."
            )
            raise MappingError(msg) from exc
    if opts.trim_whitespace:
        raw = raw.rename({c: c.strip() for c in raw.columns})
        raw = raw.with_columns(pl.col(pl.String).str.strip_chars())
    # Fully blank rows are dropped regardless of trim_whitespace: blankness is
    # judged after trimming even when values keep their padding.
    blank = pl.all_horizontal(pl.all().cast(pl.String).str.strip_chars().fill_null("").eq(""))
    n_before = raw.height
    raw = raw.filter(~blank)
    n_blank = n_before - raw.height
    # A single parsed column whose name contains another delimiter is the
    # classic wrong-delimiter signature: name the fix instead of failing later.
    if raw.width == 1:
        head = raw.columns[0]
        for cand in (",", ";", "	", "|"):
            if cand != opts.delimiter and cand in head:
                msg = (
                    f"{path}: parsed as a single column with "
                    f"{_DELIM_NAMES[opts.delimiter]} as the delimiter, but the "
                    f"header contains {_DELIM_NAMES[cand]} - the file is likely "
                    f"{_DELIM_NAMES[cand]}-delimited. Set input.delimiter: "
                    f"'{cand}' in the mapping."
                )
                raise MappingError(msg)
    return raw, n_blank


def _require_columns(df: pl.DataFrame, cols: list[str], file_label: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        msg = (
            f"{file_label}: mapped column(s) {missing} not found in the file. "
            f"Available columns: {df.columns}. Fix the mapping or the export."
        )
        raise MappingError(msg)


def load_lines(
    data_dir: Path,
    mapping: SourceMapping,
    blank_counts: dict[str, int] | None = None,
) -> pl.DataFrame:
    """Load invoice lines into the canonical schema (money in Int64 cents).
    Dropped blank-row counts are recorded per file into blank_counts."""
    path = data_dir / mapping.lines_file
    if not path.exists():
        msg = f"lines file not found: {path}"
        raise MappingError(msg)
    opts, sep = mapping.effective_input(mapping.lines_input)
    raw, n_blank = read_table(path, opts, mapping.lines_sheet)
    if blank_counts is not None and n_blank:
        blank_counts[mapping.lines_file] = n_blank
    lm = mapping.lines
    syms = opts.currency_symbols
    negf = opts.negative_format

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
        (
            pl.col(lm.quantity.column)
            .str.replace_all(".", "", literal=True)
            .str.replace_all(",", ".", literal=True)
            if sep == ","
            else pl.col(lm.quantity.column)
        )
        .cast(pl.Float64, strict=False)
        .alias("quantity"),
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
        "kit_parent_id",
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
    if lm.restocking_fee is not None:
        money_raw.append(("restocking_fee", lm.restocking_fee.column, "restocking_fee_cents"))
    if lm.return_freight_billed is not None:
        money_raw.append(
            (
                "return_freight_billed",
                lm.return_freight_billed.column,
                "return_freight_billed_cents",
            )
        )

    scaled_cols = {}
    for _canon, src, cents in money_raw:
        scaled_name = f"_{cents}_scaled"
        scaled_cols[scaled_name] = (src, cents)
    parsed = raw.select(
        [
            parse_money_expr(src, sep, syms, negf).alias(name)
            for name, (src, _) in scaled_cols.items()
        ]
    )
    check = pl.concat([raw, parsed], how="horizontal")
    for name, (src, _) in scaled_cols.items():
        assert_parse_ok(
            check,
            src,
            name,
            str(path),
            sep,
            lm.source_line_key.column,
            currency_symbols=syms,
            negative_format=negf,
        )
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
        up = raw.select(
            parse_money_expr(lm.unit_price.column, sep, syms, negf).alias("unit_price_scaled")
        )
        assert_parse_ok(
            pl.concat([raw, up], how="horizontal"),
            lm.unit_price.column,
            "unit_price_scaled",
            str(path),
            sep,
            lm.source_line_key.column,
            currency_symbols=syms,
            negative_format=negf,
        )
        out = pl.concat([out, up], how="horizontal")
        if lm.net_amount is None and lm.unit_price.authoritative:
            # net = quantity x unit price, half-even to cents, integer math.
            qty_scaled = raw.select(
                parse_money_expr(lm.quantity.column, sep, syms, negf).alias("_q")
            )["_q"]
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
        lp = raw.select(
            parse_money_expr(lm.list_price.column, sep, syms, negf).alias("list_price_scaled")
        )
        assert_parse_ok(
            pl.concat([raw, lp], how="horizontal"),
            lm.list_price.column,
            "list_price_scaled",
            str(path),
            sep,
            lm.source_line_key.column,
            currency_symbols=syms,
            negative_format=negf,
        )
        out = pl.concat([out, lp], how="horizontal")

    if lm.unit_cost is not None:
        uc = raw.select(
            parse_money_expr(lm.unit_cost.column, sep, syms, negf).alias("unit_cost_scaled")
        )
        assert_parse_ok(
            pl.concat([raw, uc], how="horizontal"),
            lm.unit_cost.column,
            "unit_cost_scaled",
            str(path),
            sep,
            lm.source_line_key.column,
            currency_symbols=syms,
            negative_format=negf,
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
    # Canonical return reason on return lines, from the declared map.
    if "credit_reason" in out.columns:
        canonical_return = (
            pl.col("credit_reason").replace_strict(mapping.return_reason_map, default="unspecified")
            if mapping.return_reason_map
            else pl.lit("unspecified")
        )
        out = out.with_columns(
            pl.when(pl.col("transaction_type") == "return")
            .then(canonical_return)
            .otherwise(None)
            .alias("return_reason")
        )
    else:
        out = out.with_columns(
            pl.when(pl.col("transaction_type") == "return")
            .then(pl.lit("unspecified"))
            .otherwise(pl.lit(None, dtype=pl.String))
            .alias("return_reason")
        )
    return out


def load_reference_table(
    data_dir: Path,
    mapping: SourceMapping,
    name: str,
    blank_counts: dict[str, int] | None = None,
) -> pl.DataFrame | None:
    """Load one reference table into canonical column names, or None if absent."""
    spec = mapping.reference_tables.get(name)
    if spec is None:
        return None
    path = data_dir / spec.file
    if not path.exists():
        return None
    opts, table_sep = mapping.effective_input(spec.input)
    raw, n_blank = read_table(path, opts, spec.sheet)
    if blank_counts is not None and n_blank:
        blank_counts[spec.file] = n_blank
    _require_columns(raw, list(spec.columns.values()), str(path))
    df = raw.select([pl.col(src).alias(canon) for canon, src in spec.columns.items()])
    # Typed columns by canonical name convention.
    money_cols = {
        "list_price": "list_price_scaled",
        "amount": "amount_cents",
        "min_amount": "min_amount_cents",
        "unit_cost": "unit_cost_cents",
        "quoted_price": "quoted_price_cents",
        "close_price": "close_price_cents",
        "inbound_freight_per_unit": "inbound_freight_pu_scaled",
        "duty_per_unit": "duty_pu_scaled",
    }
    exprs: list[pl.Expr] = []
    for canon in spec.columns:
        if canon in money_cols:
            target = money_cols[canon]
            e = parse_money_expr(
                canon,
                table_sep,
                opts.currency_symbols,
                opts.negative_format,
            )
            exprs.append(
                scaled_to_cents(e).alias(target) if target.endswith("_cents") else e.alias(target)
            )
        elif canon == "quantity":
            exprs.append(pl.col(canon).cast(pl.Int64).alias(canon))
        elif canon in ("effective_date", "start_date", "end_date", "receipt_date", "quote_date"):
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
            "rate",
        ):
            # Plain numeric columns honor the declared decimal separator too.
            num = pl.col(canon)
            if table_sep == ",":
                num = num.str.replace_all(".", "", literal=True).str.replace_all(
                    ",", ".", literal=True
                )
            exprs.append(num.cast(pl.Float64).alias(canon))
        elif canon in ("is_service", "price_protected"):
            exprs.append((pl.col(canon).cast(pl.Int64) != 0).alias(canon))
    df = df.with_columns(exprs)
    drop = [c for c in ("list_price",) if f"{c}_scaled" in df.columns and c in df.columns]
    return df.drop(drop) if drop else df
