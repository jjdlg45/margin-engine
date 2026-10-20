"""Phase 23.3 verification: input format robustness.

Every variant is built from the committed sample and must reconcile to the
clean run's ledger totals to the cent under the right mapping options - and
fail with an actionable message naming the option without them."""

from __future__ import annotations

import csv
import io
import re
import shutil
from pathlib import Path

import polars as pl
import pytest
import yaml

from margin_engine.config import Rules
from margin_engine.ingest.mapping import MappingError
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.money import MoneyParseError

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample" / "erp"
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"

MONEY_COLS = ["EXT_NET", "UNIT_PRC", "LIST_PRC", "UNIT_CST", "DISC_AMT", "REB_AMT", "FRT_CONC"]
_DECIMAL = re.compile(r"-?\d+\.\d+")


def _mapping_dict() -> dict:
    return yaml.safe_load(MAPPING.read_text(encoding="utf-8"))


def _write_mapping(tmp: Path, m: dict) -> Path:
    p = tmp / "mapping.yaml"
    p.write_text(yaml.safe_dump(m), encoding="utf-8")
    return p


def _read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _write_rows(
    path: Path,
    header: list[str],
    rows: list[list[str]],
    delimiter: str = ",",
    encoding: str = "utf-8",
) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path.write_bytes(buf.getvalue().encode(encoding))


def _copy_sample(tmp: Path) -> Path:
    erp = tmp / "erp"
    shutil.copytree(SAMPLE, erp)
    return erp


@pytest.fixture(scope="module")
def clean_totals() -> tuple[int, int]:
    res = run_pipeline(SAMPLE, MAPPING, Rules())
    return (
        int(res.ledger["net_amount_cents"].sum()),
        int(res.ledger["pocket_revenue_cents"].sum()),
    )


def _assert_totals(erp: Path, mapping_path: Path, clean: tuple[int, int]) -> None:
    res = run_pipeline(erp, mapping_path, Rules())
    assert int(res.ledger["net_amount_cents"].sum()) == clean[0]
    assert int(res.ledger["pocket_revenue_cents"].sum()) == clean[1]


def _transform_lines(erp: Path, cell_fn, header_fn=None) -> None:
    path = erp / "invoice_lines.csv"
    header, rows = _read_rows(path)
    idx = {h: i for i, h in enumerate(header)}
    for row in rows:
        for col in MONEY_COLS:
            i = idx[col]
            if row[i]:
                row[i] = cell_fn(row[i])
    if header_fn:
        header = [header_fn(h) for h in header]
    _write_rows(path, header, rows)


def test_currency_symbols(tmp_path: Path, clean_totals: tuple[int, int]) -> None:
    erp = _copy_sample(tmp_path)
    _transform_lines(erp, lambda v: f"${v}")
    m = _mapping_dict()
    m["input"] = {"currency_symbols": ["$"]}
    _assert_totals(erp, _write_mapping(tmp_path, m), clean_totals)
    # Without the option: actionable, names the mapping option.
    with pytest.raises(MoneyParseError, match="currency_symbols"):
        run_pipeline(erp, _write_mapping(tmp_path, _mapping_dict()), Rules())


def test_parentheses_negatives(tmp_path: Path, clean_totals: tuple[int, int]) -> None:
    erp = _copy_sample(tmp_path)
    _transform_lines(erp, lambda v: f"({v[1:]})" if v.startswith("-") else v)
    m = _mapping_dict()
    m["input"] = {"negative_format": "parentheses"}
    _assert_totals(erp, _write_mapping(tmp_path, m), clean_totals)
    with pytest.raises(MoneyParseError, match="negative_format"):
        run_pipeline(erp, _write_mapping(tmp_path, _mapping_dict()), Rules())


def test_semicolon_delimiter_decimal_comma(tmp_path: Path, clean_totals: tuple[int, int]) -> None:
    """Covers money, plain numeric reference columns (weight), and fractional
    quantities under the declared comma separator."""
    erp = _copy_sample(tmp_path)
    # Make one quantity fractional so the comma-decimal path is exercised on
    # quantities too (0 qty would change classification; use a .5 on a copy of
    # the value instead of altering economics: 2 -> 2.0 keeps totals).
    header, rows = _read_rows(erp / "invoice_lines.csv")
    qi = header.index("QTY_SHP")
    for row in rows[:5]:
        if row[qi] and "." not in row[qi]:
            row[qi] = f"{row[qi]}.0"
    _write_rows(erp / "invoice_lines.csv", header, rows)
    for f in erp.glob("*.csv"):
        header, rows = _read_rows(f)
        rows = [
            [_DECIMAL.sub(lambda m: m.group().replace(".", ","), v) for v in row] for row in rows
        ]
        _write_rows(f, header, rows, delimiter=";")
    m = _mapping_dict()
    m["input"] = {"delimiter": ";"}
    m["decimal_separator"] = ","
    _assert_totals(erp, _write_mapping(tmp_path, m), clean_totals)
    # Wrong delimiter: the message names the likely cause and the option.
    with pytest.raises(MappingError, match=r"input\.delimiter"):
        run_pipeline(erp, _write_mapping(tmp_path, _mapping_dict()), Rules())


def test_padded_whitespace_trimmed_by_default(
    tmp_path: Path, clean_totals: tuple[int, int]
) -> None:
    erp = _copy_sample(tmp_path)
    _transform_lines(erp, lambda v: f"  {v} ", header_fn=lambda h: f" {h}  ")
    _assert_totals(erp, _write_mapping(tmp_path, _mapping_dict()), clean_totals)


def test_utf8_bom_handled_by_default(tmp_path: Path, clean_totals: tuple[int, int]) -> None:
    erp = _copy_sample(tmp_path)
    path = erp / "invoice_lines.csv"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    _assert_totals(erp, _write_mapping(tmp_path, _mapping_dict()), clean_totals)


def test_windows_1252_and_extra_column(tmp_path: Path, clean_totals: tuple[int, int]) -> None:
    """cp1252 bytes plus an extra (unmapped) column with non-ASCII text."""
    erp = _copy_sample(tmp_path)
    path = erp / "invoice_lines.csv"
    header, rows = _read_rows(path)
    header.append("NOTES")
    for row in rows:
        row.append("entregó – ok")  # noqa: RUF001 - cp1252 bytes are the point
    _write_rows(path, header, rows, encoding="windows-1252")
    m = _mapping_dict()
    m["input"] = {"encoding": "windows-1252"}
    _assert_totals(erp, _write_mapping(tmp_path, m), clean_totals)
    with pytest.raises(MappingError, match=r"input\.encoding"):
        run_pipeline(erp, _write_mapping(tmp_path, _mapping_dict()), Rules())


def test_xlsx_lines(tmp_path: Path, clean_totals: tuple[int, int]) -> None:
    """Lines as .xlsx (money as numeric cells, the rest as text), named sheet,
    same validation path as CSV."""
    from openpyxl import Workbook

    erp = _copy_sample(tmp_path)
    header, rows = _read_rows(erp / "invoice_lines.csv")
    idx = {h: i for i, h in enumerate(header)}
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Lines"
    ws.append(header)
    money_idx = {idx[c] for c in MONEY_COLS}
    for row in rows:
        ws.append(
            [(float(v) if i in money_idx and v not in ("", None) else v) for i, v in enumerate(row)]
        )
    wb.save(erp / "invoice_lines.xlsx")
    (erp / "invoice_lines.csv").unlink()
    m = _mapping_dict()
    m["lines_file"] = "invoice_lines.xlsx"
    m["lines_sheet"] = "Lines"
    _assert_totals(erp, _write_mapping(tmp_path, m), clean_totals)
    # Wrong sheet name: actionable.
    m["lines_sheet"] = "DoesNotExist"
    with pytest.raises(MappingError, match="no sheet named"):
        run_pipeline(erp, _write_mapping(tmp_path, m), Rules())


def test_iso_dates(tmp_path: Path, clean_totals: tuple[int, int]) -> None:
    erp = _copy_sample(tmp_path)
    path = erp / "invoice_lines.csv"
    header, rows = _read_rows(path)
    i = header.index("INV_DT")
    for row in rows:
        mth, day, yr = row[i].split("/")
        row[i] = f"{yr}-{mth}-{day}"
    _write_rows(path, header, rows)
    m = _mapping_dict()
    m["lines"]["invoice_date"]["format"] = "%Y-%m-%d"
    _assert_totals(erp, _write_mapping(tmp_path, m), clean_totals)
    # Undeclared format change: actionable, names the declared format.
    with pytest.raises(MappingError, match="do not match declared"):
        run_pipeline(erp, _write_mapping(tmp_path, _mapping_dict()), Rules())


def test_renamed_and_reordered_columns(tmp_path: Path, clean_totals: tuple[int, int]) -> None:
    """Renamed + reordered source columns reconcile with an updated mapping."""
    erp = _copy_sample(tmp_path)
    path = erp / "invoice_lines.csv"
    df = pl.read_csv(path, infer_schema=False)
    renames = {c: f"X_{c}" for c in df.columns}
    df = df.rename(renames).select(sorted(renames.values()))
    df.write_csv(path)
    m = _mapping_dict()
    for spec in m["lines"].values():
        if isinstance(spec, dict) and "column" in spec:
            spec["column"] = f"X_{spec['column']}"
    _assert_totals(erp, _write_mapping(tmp_path, m), clean_totals)
