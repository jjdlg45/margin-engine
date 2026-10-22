"""Data profiler and mapping assistant (Run 4, 27.1).

`margin-engine profile <file>` scans a CSV/.xlsx export and reports, per
column: type guess, null rate, distinct count, detected date formats, and
candidate canonical fields each with a confidence and a REASON. File-level it
detects delimiter, encoding, decimal separator, sign patterns (parentheses
negatives, currency symbols) and suspected transaction-type code columns.

`margin-engine draft-mapping <file>` turns the profile into a draft mapping
YAML in which every guessed value is marked `# CONFIRM` and listed under
`unconfirmed_fields`. The engine REFUSES to run a mapping whose
`unconfirmed_fields` list is non-empty: suggestions never become silent
decisions - a person confirms each guess by deleting it from the list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import polars as pl

from margin_engine.ingest.loader import read_table
from margin_engine.ingest.mapping import InputOptions

# Encodings tried in order; the first that decodes wins (utf-8-sig eats a BOM).
_ENCODINGS = ("utf-8", "windows-1252", "latin-1")
_DELIMS = (",", ";", "\t", "|")
_CURRENCY = ("$", "€", "£")

# Date formats tried on samples. (format, human name, ambiguous-with)
_DATE_FORMATS: tuple[tuple[str, str], ...] = (
    ("%Y-%m-%d", "ISO (YYYY-MM-DD)"),
    ("%d/%m/%Y", "day-first (DD/MM/YYYY)"),
    ("%m/%d/%Y", "month-first (MM/DD/YYYY)"),
    ("%d.%m.%Y", "day-first dotted (DD.MM.YYYY)"),
    ("%Y%m%d", "compact (YYYYMMDD)"),
    ("%d-%b-%Y", "day-monthname (DD-Mon-YYYY)"),
)

# Canonical-field name heuristics: canonical -> (synonym fragments, content kind).
# Content kinds sharpen or cut confidence after a name hit.
_FIELD_HINTS: dict[str, tuple[tuple[str, ...], str]] = {
    "source_line_key": (("linekey", "lineid", "rowid", "lineno"), "id"),
    "invoice_id": (("invoice", "invno", "invid", "docno", "document"), "id"),
    "invoice_date": (("invoicedate", "date", "postingdate", "docdate"), "date"),
    "customer_id": (("customer", "custno", "custid", "account", "clientid"), "id"),
    "customer_name": (("customername", "custname", "clientname", "accountname"), "text"),
    "item_id": (("item", "sku", "product", "partno", "material"), "id"),
    "item_description": (("description", "itemdesc", "proddesc"), "text"),
    "quantity": (("qty", "quantity", "units"), "number"),
    "uom": (("uom", "unit", "measure"), "code"),
    "unit_price": (("unitprice", "price", "sellprice"), "money"),
    "net_amount": (("netamount", "amount", "extended", "linetotal", "netvalue"), "money"),
    "unit_cost": (("unitcost", "cost", "cogs"), "money"),
    "list_price": (("listprice", "list", "msrp", "baseprice"), "money"),
    "on_invoice_discount": (("discount", "disc"), "money"),
    "order_id": (("order", "orderno", "sono"), "id"),
    "transaction_type": (("type", "txntype", "doctype", "transtype"), "code"),
    "sales_rep_id": (("rep", "salesrep", "salesperson"), "id"),
    "freight_billed": (("freight", "shipping"), "money"),
    "currency": (("currency", "curr", "ccy"), "code"),
    "supplier_id": (("supplier", "vendor"), "id"),
    "item_category": (("category", "cat"), "code"),
    "item_family": (("family", "group", "productgroup"), "code"),
}


@dataclass
class FieldCandidate:
    canonical: str
    confidence: float
    reason: str


@dataclass
class ColumnProfile:
    name: str
    dtype_guess: str  # integer | number | money | date | code | text | empty
    null_rate: float
    n_distinct: int
    samples: list[str] = field(default_factory=list)
    date_formats: list[str] = field(default_factory=list)
    candidates: list[FieldCandidate] = field(default_factory=list)


@dataclass
class ProfileResult:
    path: str
    n_rows: int
    n_blank_rows: int
    encoding: str
    delimiter: str
    decimal_separator: str
    negative_parentheses: bool
    currency_symbols: list[str]
    columns: list[ColumnProfile]
    txn_type_candidates: dict[str, dict[str, int]]  # column -> code counts


def _detect_encoding(path: Path) -> str:
    blob = path.read_bytes()[:65536]
    for enc in _ENCODINGS:
        try:
            blob.decode("utf-8-sig" if enc == "utf-8" else enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _detect_delimiter(path: Path, encoding: str) -> str:
    codec = "utf-8-sig" if encoding == "utf-8" else encoding
    lines = [
        ln
        for ln in path.read_bytes()[:65536].decode(codec, errors="replace").splitlines()[:20]
        if ln.strip()
    ]
    if not lines:
        return ","

    # Quote-aware count per candidate; the winner splits the header into the
    # most columns while keeping the count consistent across sample lines.
    def counts(d: str) -> list[int]:
        out = []
        for ln in lines:
            n, in_q = 0, False
            for ch in ln:
                if ch == '"':
                    in_q = not in_q
                elif ch == d and not in_q:
                    n += 1
            out.append(n)
        return out

    best, best_cols = ",", 0
    for d in _DELIMS:
        c = counts(d)
        if not c or c[0] == 0:
            continue
        consistent = sum(1 for x in c if x == c[0]) / len(c)
        if consistent >= 0.9 and c[0] + 1 > best_cols:
            best, best_cols = d, c[0] + 1
    return best


_MONEY_RE = re.compile(r"^\(?[-+]?[\s$€£]*\d{1,3}([.,\s]\d{3})*([.,]\d+)?[\s$€£]*\)?$")
_INT_RE = re.compile(r"^[-+]?\d+$")
_NUM_RE = re.compile(r"^[-+]?\d*[.,]?\d+$")


def _non_null(vals: pl.Series) -> list[str]:
    return [v for v in vals.to_list() if v is not None and str(v).strip() != ""]


_DATE_LABEL = dict(_DATE_FORMATS)


def _date_formats_for(samples: list[str]) -> list[str]:
    """Matching strptime formats (>=90% of samples parse). More than one hit
    means the day/month order is ambiguous - the profile says so."""
    hits = []
    for fmt, _label in _DATE_FORMATS:
        ok = 0
        for v in samples:
            try:
                datetime.strptime(v.strip(), fmt)
                ok += 1
            except ValueError:
                continue
        if samples and ok / len(samples) >= 0.9:
            hits.append(fmt)
    return hits


def _profile_column(name: str, vals: pl.Series) -> ColumnProfile:
    nn = _non_null(vals)
    total = max(len(vals), 1)
    null_rate = 1 - len(nn) / total
    distinct = len(set(nn))
    samples = sorted(set(nn))[:5]
    probe = nn[:200]
    date_fmts = _date_formats_for(probe) if probe else []
    if not nn:
        dtype = "empty"
    elif date_fmts:
        dtype = "date"
    elif all(_INT_RE.match(v.strip()) for v in probe):
        dtype = "integer"
    elif all(_MONEY_RE.match(v.strip()) for v in probe) and any(
        ("." in v or "," in v or any(c in v for c in _CURRENCY) or "(" in v) for v in probe
    ):
        dtype = "money"
    elif all(_NUM_RE.match(v.strip()) for v in probe):
        dtype = "number"
    elif distinct <= 20 and max(len(v) for v in probe) <= 12:
        dtype = "code"
    else:
        dtype = "text"
    return ColumnProfile(
        name=name,
        dtype_guess=dtype,
        null_rate=round(null_rate, 4),
        n_distinct=distinct,
        samples=samples,
        date_formats=date_fmts,
    )


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_CONTENT_OK = {
    "id": ("integer", "code", "text"),
    "date": ("date",),
    "money": ("money", "number", "integer"),
    "number": ("number", "integer", "money"),
    "code": ("code", "text", "integer"),
    "text": ("text", "code"),
}


def _candidates_for(col: ColumnProfile) -> list[FieldCandidate]:
    n = _norm(col.name)
    out: list[FieldCandidate] = []
    for canonical, (fragments, kind) in _FIELD_HINTS.items():
        hit = next((f for f in fragments if f in n), None)
        if hit is None:
            continue
        conf = 0.9 if n == hit else 0.7
        reason = (
            f"header '{col.name}' matches '{hit}'"
            if n != hit
            else f"header '{col.name}' equals the synonym '{hit}'"
        )
        if col.dtype_guess in _CONTENT_OK[kind]:
            reason += f"; content looks like {col.dtype_guess} (expected {kind})"
        else:
            conf -= 0.4
            reason += f"; BUT content looks like {col.dtype_guess}, not {kind} - verify carefully"
        if conf > 0.2:
            out.append(
                FieldCandidate(canonical=canonical, confidence=round(conf, 2), reason=reason)
            )
    out.sort(key=lambda c: -c.confidence)
    return out[:3]


def profile_file(path: Path, sheet: str | int | None = None) -> ProfileResult:
    """Profile a CSV/.xlsx export: formats, per-column stats, candidates."""
    if path.suffix.lower() == ".xlsx":
        encoding, delim = "utf-8", ","
        opts = InputOptions()
    else:
        encoding = _detect_encoding(path)
        delim = _detect_delimiter(path, encoding)
        opts = InputOptions(
            delimiter=delim,  # type: ignore[arg-type]
            encoding=encoding,
            negative_format="minus",
        )
    raw, n_blank = read_table(path, opts, sheet)

    columns = [_profile_column(c, raw[c]) for c in raw.columns]
    for col in columns:
        col.candidates = _candidates_for(col)

    # Decimal separator: money/number columns where values look like 1.234,56.
    comma_decimal = 0
    dot_decimal = 0
    for col in columns:
        if col.dtype_guess not in ("money", "number"):
            continue
        for v in col.samples:
            if re.search(r"\d\.\d{3}(,\d+)$", v) or re.search(r"\d(,\d{1,2})$", v):
                comma_decimal += 1
            elif re.search(r"\.\d{1,2}$", v):
                dot_decimal += 1
    decimal_sep = "," if comma_decimal > dot_decimal else "."

    # Sign patterns and currency symbols across money columns.
    paren = False
    symbols: set[str] = set()
    for col in columns:
        if col.dtype_guess != "money":
            continue
        for v in col.samples:
            if v.strip().startswith("(") and v.strip().endswith(")"):
                paren = True
            for c in _CURRENCY:
                if c in v:
                    symbols.add(c)

    # Transaction-type candidates: short-code columns with few distinct values.
    txn: dict[str, dict[str, int]] = {}
    for col in columns:
        if col.dtype_guess == "code" and 2 <= col.n_distinct <= 20:
            vc = raw[col.name].value_counts().sort(col.name).drop_nulls().to_dicts()
            txn[col.name] = {str(r[col.name]): int(r["count"]) for r in vc[:20]}

    return ProfileResult(
        path=str(path),
        n_rows=raw.height,
        n_blank_rows=n_blank,
        encoding=encoding,
        delimiter=delim,
        decimal_separator=decimal_sep,
        negative_parentheses=paren,
        currency_symbols=sorted(symbols),
        columns=columns,
        txn_type_candidates=txn,
    )


def render_profile(p: ProfileResult) -> str:
    """Human-readable profile report."""
    delim_name = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}[p.delimiter]
    lines = [
        f"Profile of {p.path}",
        f"  rows: {p.n_rows:,} (plus {p.n_blank_rows} blank rows dropped)",
        f"  encoding: {p.encoding}; delimiter: {delim_name}; "
        f"decimal separator: '{p.decimal_separator}'",
        f"  negatives in parentheses: {'yes' if p.negative_parentheses else 'no'}; "
        f"currency symbols: {', '.join(p.currency_symbols) or 'none'}",
        "",
        "Columns:",
    ]
    for c in p.columns:
        lines.append(
            f"  {c.name}: {c.dtype_guess}, null rate {c.null_rate:.1%}, "
            f"{c.n_distinct:,} distinct"
            + (
                f", date format {' OR '.join(_DATE_LABEL[f] for f in c.date_formats)}"
                + (" - AMBIGUOUS, confirm day/month order" if len(c.date_formats) > 1 else "")
                if c.date_formats
                else ""
            )
        )
        for cand in c.candidates:
            lines.append(f"      -> {cand.canonical} ({cand.confidence:.0%}): {cand.reason}")
    if p.txn_type_candidates:
        lines.append("")
        lines.append("Suspected transaction-type code columns:")
        for col, codes in p.txn_type_candidates.items():
            pairs = ", ".join(f"{k} x{v}" for k, v in list(codes.items())[:8])
            lines.append(f"  {col}: {pairs}")
    lines.append("")
    lines.append(
        "Every guess above needs human confirmation; run "
        "`margin-engine draft-mapping` to start a mapping from this profile."
    )
    return "\n".join(lines)


def draft_mapping(p: ProfileResult) -> str:
    """A draft mapping YAML in the engine's real schema: every guessed value
    marked # CONFIRM and listed in unconfirmed_fields. The engine refuses to
    run until that list is empty - suggestions never become silent decisions."""
    best: dict[str, tuple[str, FieldCandidate, ColumnProfile]] = {}
    for col in p.columns:
        for cand in col.candidates:
            cur = best.get(cand.canonical)
            if cur is None or cand.confidence > cur[1].confidence:
                best[cand.canonical] = (col.name, cand, col)

    unconfirmed: list[str] = [
        "input.delimiter",
        "input.encoding",
        "input.negative_format",
        "decimal_separator",
    ]

    def guess(canonical: str) -> tuple[str, str] | None:
        hit = best.get(canonical)
        if hit is None:
            return None
        src, cand, _col = hit
        return src, f"# CONFIRM ({cand.confidence:.0%}) {cand.reason}"

    def key_line(canonical: str, indent: str = "  ") -> list[str]:
        g = guess(canonical)
        unconfirmed.append(f"lines.{canonical}")
        if g is None:
            return [
                f"{indent}{canonical}:",
                f"{indent}  column: FIXME_UNKNOWN  # CONFIRM - no candidate detected",
            ]
        src, note = g
        return [f"{indent}{canonical}:", f"{indent}  column: {src}  {note}"]

    delim_yaml = '"\\t"' if p.delimiter == "\t" else f'"{p.delimiter}"'
    lines = [
        "# Draft mapping generated by `margin-engine draft-mapping` - NOT runnable",
        "# as-is: every guessed value is marked # CONFIRM with its confidence and",
        "# reason. A person confirms a guess by reviewing it and DELETING its entry",
        "# from unconfirmed_fields at the bottom; the engine refuses to run while",
        "# any entry remains. Business meaning (cost basis, sign conventions,",
        "# transaction codes) can never be guessed from data - declare it.",
        f"# Source profiled: {p.path}",
        "",
        f"source_system: DRAFT_{Path(p.path).stem}",
        f"lines_file: {Path(p.path).name}",
        f'decimal_separator: "{p.decimal_separator}"  # CONFIRM (detected from samples)',
        "input:",
        f"  delimiter: {delim_yaml}  # CONFIRM (detected)",
        f"  encoding: {p.encoding}  # CONFIRM (first codec that decoded)",
        f"  negative_format: {'parentheses' if p.negative_parentheses else 'minus'}"
        "  # CONFIRM (from money samples)",
    ]
    if p.currency_symbols:
        syms = ", ".join(f'"{sym}"' for sym in p.currency_symbols)
        lines.append(f"  currency_symbols: [{syms}]  # CONFIRM (seen in money samples)")
        unconfirmed.append("input.currency_symbols")
    lines.append("lines:")
    for canonical in ("source_line_key", "invoice_id", "customer_id", "item_id"):
        lines += key_line(canonical)

    g = guess("invoice_date")
    unconfirmed.append("lines.invoice_date")
    date_col = best.get("invoice_date")
    fmts = date_col[2].date_formats if date_col else []
    fmt = fmts[0] if fmts else "%Y-%m-%d"
    ambiguous = (
        "  # CONFIRM - AMBIGUOUS: samples also parse as "
        + " / ".join(_DATE_LABEL[f] for f in fmts[1:])
        if len(fmts) > 1
        else "  # CONFIRM (detected)"
    )
    if g is not None:
        lines += [
            "  invoice_date:",
            f"    column: {g[0]}  {g[1]}",
            f'    format: "{fmt}"{ambiguous}',
        ]
    else:
        lines += [
            "  invoice_date:",
            "    column: FIXME_UNKNOWN  # CONFIRM - no candidate detected",
            '    format: "%Y-%m-%d"  # CONFIRM',
        ]

    lines += key_line("quantity")

    g = guess("net_amount") or guess("unit_price")
    unconfirmed += ["lines.net_amount.currency", "lines.net_amount.sign_convention"]
    lines += [
        "  net_amount:",
        f"    column: {g[0]}  {g[1]}" if g else "    column: FIXME_UNKNOWN  # CONFIRM",
        "    currency: USD  # CONFIRM - business meaning, never guessable",
        "    sign_convention: natural  # CONFIRM (natural / positive_reduces_revenue /",
        "    # always_positive_with_type)",
    ]

    g = guess("unit_cost")
    if g is not None:
        unconfirmed += [
            "lines.unit_cost.cost_basis",
            "lines.unit_cost.currency",
            "lines.unit_cost.per_uom",
        ]
        lines += [
            "  unit_cost:",
            f"    column: {g[0]}  {g[1]}",
            "    currency: USD  # CONFIRM",
            "    # The cost BASIS cannot be guessed from data: standard / average /",
            "    # fifo / lifo / landed, and what the figure already includes.",
            "    cost_basis: average  # CONFIRM - business meaning, never guessable",
            "    includes_inbound_freight: false  # CONFIRM",
            "    includes_duty: false  # CONFIRM",
            "    includes_vendor_rebates: false  # CONFIRM",
            "    per_uom: line_uom  # CONFIRM (line_uom / base_uom)",
        ]

    if p.txn_type_candidates:
        tcol = next(iter(p.txn_type_candidates))
        codes = list(p.txn_type_candidates[tcol])
        unconfirmed.append("lines.transaction_type.code_map")
        lines += [
            "  transaction_type:",
            f"    column: {tcol}  # CONFIRM (code column; values: {', '.join(codes[:6])})",
            "    code_map:",
        ]
        for code in codes[:8]:
            lines.append(f'      "{code}": sale  # CONFIRM - map to sale / return / credit')
        lines.append("    reversal_handling: own_code  # CONFIRM (own_code / negative_amounts)")

    lines += [
        "",
        "# Delete entries one by one as a person confirms each guess above.",
        "unconfirmed_fields:",
        *[f"  - {u}" for u in sorted(set(unconfirmed))],
        "",
    ]
    return "\n".join(lines)
