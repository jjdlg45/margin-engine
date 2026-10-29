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

from margin_engine.ingest.mapping import (
    InputOptions,
    MappingError,
    SourceMapping,
    TransactionTypeDerived,
)
from margin_engine.money import (
    PARSE_SCALE,
    MoneyParseError,
    normalize_money_expr,
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

        # Hidden rows (Run 6, Phase 85). A hidden row is still data, and
        # dropping it would be a decision nobody made - a filtered export can
        # hide the very lines under review. They are KEPT and counted, so the
        # report can say how many were hidden and a reviewer can decide
        # whether that was deliberate.
        hidden = _count_hidden_rows(path)
        if hidden:
            _HIDDEN_ROWS[str(path)] = hidden

        rows = ws.iter_rows(values_only=True)
        try:
            header = [str(h) if h is not None else "" for h in next(rows)]
        except StopIteration:
            msg = f"{path}: sheet '{ws.title}' is empty."
            raise MappingError(msg) from None
        data: dict[str, list[str | None]] = {h: [] for h in header}
        any_null = False
        for row in rows:
            for h, v in zip(header, row, strict=False):
                if v is None:
                    any_null = True
                data[h].append(cell_str(v))
        if any_null:
            # Formulas without cached results (Run 6, Phase 85). data_only
            # returns the value Excel last computed; a workbook written by a
            # tool that never computed them (openpyxl, some exporters) yields
            # None for every formula cell, indistinguishable here from a
            # genuinely blank cell. Blank cells are everywhere in real
            # exports, so the second pass runs only when the first saw nulls,
            # and only to ask one question: were any of them formulas?
            _raise_if_uncomputed_formulas(path, ws.title)
        return pl.DataFrame(data, schema=dict.fromkeys(header, pl.String))
    finally:
        wb.close()


def _raise_if_uncomputed_formulas(path: Path, sheet_title: str) -> None:
    """Fail actionably when the workbook holds formulas Excel never computed.

    Reading them as blanks would turn '=B2*C2' into a missing amount and the
    engine would then honestly report revenue that is wrong at the source -
    honest arithmetic on dishonest input. The message says what to do.
    """
    from openpyxl import load_workbook

    wb2 = load_workbook(path, read_only=True, data_only=False)
    try:
        ws2 = next((w for w in wb2.worksheets if w.title == sheet_title), wb2.worksheets[0])
        n_formulas = 0
        example = ""
        for row in ws2.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.startswith("="):
                    n_formulas += 1
                    if not example:
                        example = f"{cell.coordinate}: {v[:40]}"
        if n_formulas:
            msg = (
                f"{path}: sheet '{sheet_title}' holds {n_formulas} formula cell(s) "
                f"with no computed values stored (first: {example}). The engine "
                "reads values, never evaluates formulas - open the workbook and "
                "save it so the results are stored, or export the sheet to CSV."
            )
            raise MappingError(msg)
    finally:
        wb2.close()


#: Hidden spreadsheet rows seen on the last read, by file. Read by the
#: pipeline for its notes; a dict rather than a return value because the
#: xlsx reader sits several layers below the caller that reports.
_HIDDEN_ROWS: dict[str, int] = {}


def hidden_rows_seen() -> dict[str, int]:
    """Hidden rows encountered since the last reset, by file path."""
    return dict(_HIDDEN_ROWS)


def reset_hidden_rows() -> None:
    _HIDDEN_ROWS.clear()


#: Excel's 1900 date system: 1 is 1900-01-01, and anything a business export
#: plausibly contains sits roughly between 1990 and 2100.
_SERIAL_RANGE = (32_000, 74_000)


def _count_hidden_rows(path: Path) -> int:
    """Hidden data rows across the workbook's sheets.

    openpyxl's read-only worksheets - which the loader uses, because holding
    a 10M-line workbook in the cell model is not an option - do not expose
    row dimensions. The flag lives in the sheet XML as `hidden="1"` on the
    row element, so it is read straight from the zip: cheap, streaming, and
    it does not force the whole workbook back into memory to answer one
    question. Header rows are not counted; neither is a failure to read,
    which returns zero rather than inventing a number.
    """
    import re
    import zipfile

    pattern = re.compile(rb'<row[^>]*\bhidden="(?:1|true)"', re.IGNORECASE)
    total = 0
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                    continue
                with zf.open(name) as fh:
                    tail = b""
                    while chunk := fh.read(1 << 20):
                        buf = tail + chunk
                        total += len(pattern.findall(buf))
                        # Keep a tail so a row tag split across chunks is
                        # still seen exactly once.
                        tail = buf[-200:]
    except (OSError, zipfile.BadZipFile):
        return 0
    return total


def _serial_date_hint(samples: list[object]) -> str:
    """Name the spreadsheet-serial case when the bad values look like one.

    An Excel cell formatted as General holds a date as a day count - 45678,
    not 2025-01-15 - and openpyxl returns the number. Without this, the
    actionable message says only that the values do not match the declared
    format, which is true and unhelpful: the fix is in the workbook, not the
    mapping.
    """
    numeric = [
        str(v)
        for v in samples
        if str(v).strip().isdigit() and _SERIAL_RANGE[0] <= int(str(v).strip()) <= _SERIAL_RANGE[1]
    ]
    if not numeric:
        return ""
    return (
        f" Values like {numeric[:3]} are spreadsheet date SERIAL numbers, not "
        "dates: the cells are formatted as General, so the workbook stores a "
        "day count. Format the column as a date and save, or export to CSV "
        "with the dates written out - the engine never guesses a date system."
    )


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
        is_utf8 = opts.encoding.lower().replace("_", "-") in ("utf-8", "utf8")
        if is_utf8:
            # Fast path (27.2): let polars read the file directly instead of
            # holding bytes + decoded text + a re-encoded copy in memory at
            # once - at 10M lines those three copies alone are several GB.
            # polars strips a UTF-8 BOM itself; invalid bytes surface as a
            # ComputeError that we turn into the same actionable message.
            try:
                raw = pl.read_csv(
                    path,
                    infer_schema=False,
                    separator=opts.delimiter,
                    quote_char='"',
                )
            except pl.exceptions.ComputeError as exc:
                if "utf" in str(exc).lower() or "invalid" in str(exc).lower():
                    msg = (
                        f"{path}: cannot decode as '{opts.encoding}' ({exc}). The "
                        f"file is probably in another encoding (Windows exports "
                        f"are often windows-1252); set input.encoding in the mapping."
                    )
                    raise MappingError(msg) from exc
                msg = (
                    f"{path}: could not be parsed as {_DELIM_NAMES[opts.delimiter]}-"
                    f"delimited CSV ({exc}). Check input.delimiter and "
                    f"input.encoding in the mapping."
                )
                raise MappingError(msg) from exc
            except Exception as exc:
                msg = (
                    f"{path}: could not be parsed as {_DELIM_NAMES[opts.delimiter]}-"
                    f"delimited CSV ({exc}). Check input.delimiter and "
                    f"input.encoding in the mapping."
                )
                raise MappingError(msg) from exc
            # A UTF-8 BOM, if present, must not leak into the first header.
            if raw.width and raw.columns[0].startswith("﻿"):
                raw = raw.rename({raw.columns[0]: raw.columns[0].lstrip("﻿")})
        else:
            blob = path.read_bytes()
            try:
                text = blob.decode(opts.encoding)
            except (UnicodeDecodeError, LookupError) as exc:
                msg = (
                    f"{path}: cannot decode as '{opts.encoding}' ({exc}). The file is "
                    f"probably in another encoding (Windows exports are often "
                    f"windows-1252); set input.encoding in the mapping."
                )
                raise MappingError(msg) from exc
            del blob
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
            del text
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


def _scan_lines(
    path: Path, opts: InputOptions, sheet: str | int | None
) -> tuple[pl.LazyFrame, int | None]:
    """Lazy all-string view of the lines file (Phase 35).

    UTF-8 CSV scans lazily (the raw string frame never fully materializes;
    validation runs as streaming aggregations). .xlsx and non-UTF-8 files take
    the eager `read_table` path — those readers decode in memory anyway — and
    return a .lazy() view with blanks already dropped and counted.
    Returns (lazy frame, n_blank_rows) where n_blank is None when the blank
    filter still has to run lazily (the UTF-8 CSV path).
    """
    is_utf8 = opts.encoding.lower().replace("_", "-") in ("utf-8", "utf8")
    if path.suffix.lower() == ".xlsx" or not is_utf8:
        frame, n_blank = read_table(path, opts, sheet)
        return frame.lazy(), n_blank
    try:
        lf = pl.scan_csv(path, infer_schema=False, separator=opts.delimiter, quote_char='"')
        names = lf.collect_schema().names()
    except pl.exceptions.ComputeError as exc:
        if "utf" in str(exc).lower() or "invalid" in str(exc).lower():
            msg = (
                f"{path}: cannot decode as '{opts.encoding}' ({exc}). The "
                f"file is probably in another encoding (Windows exports "
                f"are often windows-1252); set input.encoding in the mapping."
            )
            raise MappingError(msg) from exc
        msg = (
            f"{path}: could not be parsed as {_DELIM_NAMES[opts.delimiter]}-"
            f"delimited CSV ({exc}). Check input.delimiter and "
            f"input.encoding in the mapping."
        )
        raise MappingError(msg) from exc
    except Exception as exc:
        msg = (
            f"{path}: could not be parsed as {_DELIM_NAMES[opts.delimiter]}-"
            f"delimited CSV ({exc}). Check input.delimiter and "
            f"input.encoding in the mapping."
        )
        raise MappingError(msg) from exc
    # A UTF-8 BOM, if present, must not leak into the first header.
    if names and names[0].startswith("﻿"):
        lf = lf.rename({names[0]: names[0].lstrip("﻿")})
        names = [names[0].lstrip("﻿"), *names[1:]]
    if opts.trim_whitespace:
        lf = lf.rename({c: c.strip() for c in names})
        lf = lf.with_columns(pl.col(pl.String).str.strip_chars())
        names = [c.strip() for c in names]
    # Wrong-delimiter signature: one parsed column whose name holds another
    # delimiter. Named fix instead of a confusing failure later.
    if len(names) == 1:
        head = names[0]
        for cand in (",", ";", "\t", "|"):
            if cand != opts.delimiter and cand in head:
                msg = (
                    f"{path}: parsed as a single column with "
                    f"{_DELIM_NAMES[opts.delimiter]} as the delimiter, but the "
                    f"header contains {_DELIM_NAMES[cand]} - the file is likely "
                    f"{_DELIM_NAMES[cand]}-delimited. Set input.delimiter: "
                    f"'{cand}' in the mapping."
                )
                raise MappingError(msg)
    return lf, None


def _collect(
    lf: pl.LazyFrame, path: Path, opts: InputOptions, *, streaming: bool = False
) -> pl.DataFrame:
    """Collect with scan errors translated to the same actionable messages the
    eager reader produced (a lazy CSV scan surfaces decode errors here)."""
    try:
        return lf.collect(engine="streaming") if streaming else lf.collect()
    except pl.exceptions.ComputeError as exc:
        if "utf" in str(exc).lower() or "invalid" in str(exc).lower():
            msg = (
                f"{path}: cannot decode as '{opts.encoding}' ({exc}). The "
                f"file is probably in another encoding (Windows exports "
                f"are often windows-1252); set input.encoding in the mapping."
            )
            raise MappingError(msg) from exc
        msg = (
            f"{path}: could not be parsed as {_DELIM_NAMES[opts.delimiter]}-"
            f"delimited CSV ({exc}). Check input.delimiter and "
            f"input.encoding in the mapping."
        )
        raise MappingError(msg) from exc


def load_lines(
    data_dir: Path,
    mapping: SourceMapping,
    blank_counts: dict[str, int] | None = None,
) -> pl.DataFrame:
    """Load invoice lines into the canonical schema (money in Int64 cents).
    Dropped blank-row counts are recorded per file into blank_counts.

    Phase 35: the source is consumed as a lazy scan with streaming collection —
    validation (blank rows, unparseable money or dates, undeclared transaction
    codes) runs as aggregations over the scan, and only the typed canonical
    frame is ever materialized. Values, column order and error messages are
    identical to the eager loader this replaces.
    """
    path = data_dir / mapping.lines_file
    if not path.exists():
        msg = f"lines file not found: {path}"
        raise MappingError(msg)
    opts, sep = mapping.effective_input(mapping.lines_input)
    lf, n_blank_eager = _scan_lines(path, opts, mapping.lines_sheet)
    lm = mapping.lines
    syms = opts.currency_symbols
    negf = opts.negative_format

    names = lf.collect_schema().names()
    declared: list[str] = []
    for f in type(lm).model_fields:
        spec = getattr(lm, f)
        if spec is not None:
            declared.append(
                spec.derive_from_column if isinstance(spec, TransactionTypeDerived) else spec.column
            )
    missing_cols = [c for c in declared if c not in names]
    if missing_cols:
        msg = (
            f"{path}: mapped column(s) {missing_cols} not found in the file. "
            f"Available columns: {names}. Fix the mapping or the export."
        )
        raise MappingError(msg)

    # Fully blank rows are dropped regardless of trim_whitespace: blankness is
    # judged after trimming (on every column) even when values keep padding.
    if n_blank_eager is None:
        blank = pl.all_horizontal(pl.all().cast(pl.String).str.strip_chars().fill_null("").eq(""))
        n_blank = int(_collect(lf.select(blank.sum().alias("n")), path, opts)["n"].item() or 0)
        if n_blank:
            lf = lf.filter(~blank)
    else:
        n_blank = n_blank_eager
    if blank_counts is not None and n_blank:
        blank_counts[mapping.lines_file] = n_blank

    # --- validation pass: one streaming scan computes every bad-value count ---
    date_expr = pl.col(lm.invoice_date.column).str.to_date(lm.invoice_date.format, strict=False)
    checks: list[pl.Expr] = [date_expr.is_null().sum().alias("_bad_dates")]

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
    if lm.tax_amount is not None:
        money_raw.append(("tax_amount", lm.tax_amount.column, "tax_cents"))
    if lm.return_freight_billed is not None:
        money_raw.append(
            (
                "return_freight_billed",
                lm.return_freight_billed.column,
                "return_freight_billed_cents",
            )
        )
    # Per-unit price columns at parse scale join the same validation pass.
    scaled_specs: list[tuple[str, str]] = []  # (source col, output scaled col)
    if lm.unit_price is not None:
        scaled_specs.append((lm.unit_price.column, "unit_price_scaled"))
    if lm.list_price is not None:
        scaled_specs.append((lm.list_price.column, "list_price_scaled"))
    if lm.unit_cost is not None:
        scaled_specs.append((lm.unit_cost.column, "unit_cost_scaled"))

    money_srcs = [src for _, src, _ in money_raw] + [src for src, _ in scaled_specs]
    for i, src in enumerate(money_srcs):
        parsed = parse_money_expr(src, sep, syms, negf)
        nonempty = pl.col(src).cast(pl.String).str.strip_chars().fill_null("").ne("")
        checks.append((nonempty & parsed.is_null()).sum().alias(f"_bad_money_{i}"))
        normalized = normalize_money_expr(pl.col(src), sep, syms, negf)
        too_precise = (
            normalized.str.split_exact(".", 1)
            .struct.field("field_1")
            .fill_null("")
            .str.len_chars()
            .gt(PARSE_SCALE)
        )
        checks.append(too_precise.sum().alias(f"_too_precise_{i}"))

    txn_spec = lm.transaction_type
    if txn_spec is None:
        msg = (
            f"mapping '{mapping.source_system}' declares no transaction_type and no "
            f"derivation rule; cannot classify sales vs returns vs credits. "
            f"Add a transaction_type entry with a code_map."
        )
        raise MappingError(msg)
    code_map: dict[str, str] | None = None
    if not isinstance(txn_spec, TransactionTypeDerived):
        code_map = dict(txn_spec.code_map)
        txn_col = txn_spec.column
        checks.append(
            (~pl.col(txn_col).is_in(list(txn_spec.code_map.keys())) & pl.col(txn_col).is_not_null())
            .sum()
            .alias("_bad_codes")
        )

    counts = _collect(lf.select(checks), path, opts, streaming=True).row(0, named=True)

    def _offenders(cond: pl.Expr, col: str) -> pl.DataFrame:
        return _collect(lf.filter(cond).select(pl.col(col)).head(10), path, opts)

    key_src = lm.source_line_key.column
    if counts["_bad_dates"]:
        bad_date_samples = (
            _offenders(date_expr.is_null(), lm.invoice_date.column)[lm.invoice_date.column]
            .head(5)
            .to_list()
        )
        keys = _offenders(date_expr.is_null(), key_src)[key_src].to_list()
        msg = (
            f"{path}: {counts['_bad_dates']} invoice_date value(s) do not match declared "
            f"format '{lm.invoice_date.format}'. Offending line keys (up to 10): {keys}"
            + _serial_date_hint(bad_date_samples)
        )
        raise MappingError(msg)
    for i, src in enumerate(money_srcs):
        parsed = parse_money_expr(src, sep, syms, negf)
        nonempty = pl.col(src).cast(pl.String).str.strip_chars().fill_null("").ne("")
        bad_cond = nonempty & parsed.is_null()
        if counts[f"_bad_money_{i}"]:
            sample = _offenders(bad_cond, src)[src].head(5).to_list()
            keys = _offenders(bad_cond, key_src)[key_src].to_list()
            msg = (
                f"{path}: column '{src}' has {counts[f'_bad_money_{i}']} unparseable money "
                f"value(s), e.g. {sample}. Likely causes: a currency symbol not listed "
                f"in the mapping's input.currency_symbols, parentheses negatives without "
                f"input.negative_format: parentheses, or the wrong decimal_separator."
                f" Offending line keys (up to 10): {keys}."
            )
            raise MoneyParseError(msg)
        if counts[f"_too_precise_{i}"]:
            normalized = normalize_money_expr(pl.col(src), sep, syms, negf)
            too_precise = (
                normalized.str.split_exact(".", 1)
                .struct.field("field_1")
                .fill_null("")
                .str.len_chars()
                .gt(PARSE_SCALE)
            )
            keys = _offenders(too_precise, key_src)[key_src].to_list()
            msg = (
                f"{path}: column '{src}' has values with more than "
                f"{PARSE_SCALE} decimal places; refusing to truncate money."
                f" Offending line keys (up to 10): {keys}."
            )
            raise MoneyParseError(msg)
    if (
        code_map is not None
        and not isinstance(txn_spec, TransactionTypeDerived)
        and counts.get("_bad_codes")
    ):
        txn_col = txn_spec.column
        bad_cond = ~pl.col(txn_col).is_in(list(code_map.keys())) & pl.col(txn_col).is_not_null()
        codes_df = _collect(
            lf.filter(bad_cond).select(pl.col(txn_col).alias("_c")).unique().sort("_c").head(10),
            path,
            opts,
        )
        codes = codes_df["_c"].to_list()
        msg = (
            f"{path}: transaction codes {codes} not declared in the mapping's "
            f"code_map {code_map}. Declare every code; the engine never guesses."
        )
        raise MappingError(msg)

    # --- the canonical frame: one ordered select, streamed into memory --------
    exprs: list[pl.Expr] = [
        pl.col(lm.source_line_key.column).alias("source_line_key"),
        pl.col(lm.invoice_id.column).alias("invoice_id"),
        date_expr.alias("invoice_date"),
        # Blank identity is missing identity, never a key of its own: real
        # exports carry anonymous/guest sales (28.3), and an "" customer would
        # otherwise aggregate into one giant fake account.
        pl.col(lm.customer_id.column).replace("", None).alias("customer_id"),
        pl.col(lm.item_id.column).replace("", None).alias("item_id"),
        (
            pl.col(lm.quantity.column)
            .str.replace_all(".", "", literal=True)
            .str.replace_all(",", ".", literal=True)
            if sep == ","
            else pl.col(lm.quantity.column)
        )
        .cast(pl.Float64, strict=False)
        .alias("quantity"),
    ]
    if lm.uom is not None:
        exprs.append(pl.col(lm.uom.column).alias("uom"))
    if lm.order_id is not None:
        exprs.append(pl.col(lm.order_id.column).alias("order_id"))
    else:
        # Real exports often carry no order number: the invoice is the natural
        # order proxy (one document, one shipment). Stated in the run notes.
        exprs.append(pl.col(lm.invoice_id.column).alias("order_id"))
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
            e = pl.col(spec.column).replace("", None)
            if key_field == "credit_reason" and mapping.credit_reason_map:
                # Normalize credit reasons through the declared map (unmapped
                # reasons kept raw).
                e = e.replace(mapping.credit_reason_map)
            exprs.append(e.alias(key_field))
        elif key_field in ("credit_reason", "contract_id"):
            # Always present in the canonical frame: downstream stages read
            # them (credit classification; contract regime in peer groups,
            # segmentation, guidance and the panel), and a mapping without
            # the column must not crash - null means "none declared".
            exprs.append(pl.lit(None, dtype=pl.String).alias(key_field))
    if lm.return_disposition is not None:
        disp = pl.col(lm.return_disposition.column).replace("", None)
        if mapping.return_disposition_map:
            disp = disp.replace(mapping.return_disposition_map)
        exprs.append(disp.alias("return_disposition"))

    # --- money fields, digit-exact ---------------------------------------
    for _canon, src, cents in money_raw:
        exprs.append(scaled_to_cents(parse_money_expr(src, sep, syms, negf)).alias(cents))

    # Unit price and list price: per-unit values at parse scale (ten-thousandths).
    if lm.unit_price is not None:
        exprs.append(
            parse_money_expr(lm.unit_price.column, sep, syms, negf).alias("unit_price_scaled")
        )
        if lm.net_amount is None and lm.unit_price.authoritative:
            # net = quantity x unit price, half-even to cents, integer math.
            exprs.append(
                scaled_to_cents(
                    (
                        parse_money_expr(lm.quantity.column, sep, syms, negf)
                        * parse_money_expr(lm.unit_price.column, sep, syms, negf)
                    ).floordiv(10_000)
                ).alias("net_amount_cents")
            )
    if lm.list_price is not None:
        exprs.append(
            parse_money_expr(lm.list_price.column, sep, syms, negf).alias("list_price_scaled")
        )
    if lm.unit_cost is not None:
        uc_expr = parse_money_expr(lm.unit_cost.column, sep, syms, negf)
        if lm.unit_cost.missing_as == "zero_is_missing":
            uc_expr = pl.when(uc_expr == 0).then(None).otherwise(uc_expr)
        exprs.append(uc_expr.alias("unit_cost_scaled"))
    else:
        exprs.append(pl.lit(None, dtype=pl.Int64).alias("unit_cost_scaled"))

    # --- transaction type --------------------------------------------------
    if isinstance(txn_spec, TransactionTypeDerived):
        # Configured derivation rule (5.2): longest matching prefix wins; no
        # match (or a null key) takes the declared default. Build the when-
        # chain shortest-first so the longest prefix ends up outermost.
        txn_expr: pl.Expr = pl.lit(txn_spec.default)
        for prefix, typ in sorted(txn_spec.prefix_map.items(), key=lambda kv: len(kv[0])):
            txn_expr = (
                pl.when(pl.col(txn_spec.derive_from_column).str.starts_with(prefix))
                .then(pl.lit(typ))
                .otherwise(txn_expr)
            )
    else:
        txn_expr = pl.col(txn_spec.column).replace_strict(txn_spec.code_map, default=None)
    exprs.append(txn_expr.alias("transaction_type"))

    # Canonical return reason on return lines, from the declared map. The
    # credit_reason expression mirrors the canonical column built above.
    reason_spec = lm.credit_reason
    if reason_spec is not None:
        reason_src = pl.col(reason_spec.column).replace("", None)
        if mapping.credit_reason_map:
            reason_src = reason_src.replace(mapping.credit_reason_map)
        canonical_return = (
            reason_src.replace_strict(mapping.return_reason_map, default="unspecified")
            if mapping.return_reason_map
            else pl.lit("unspecified")
        )
    else:
        canonical_return = pl.lit("unspecified")
    exprs.append(
        pl.when(txn_expr == "return")
        .then(canonical_return)
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("return_reason")
    )

    return _collect(lf.select(exprs), path, opts, streaming=True)


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
        # Books tie-out (27.3): GL monthly totals.
        "net_sales": "net_sales_cents",
        "cogs": "cogs_cents",
        "returns": "returns_cents",
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
            "market_index",
            "value",
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
