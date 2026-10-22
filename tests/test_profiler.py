"""Run 4 (27.1): data profiler and mapping assistant. The profiler detects
formats and candidates with reasons; the draft mapping carries # CONFIRM
markers and a machine-readable unconfirmed_fields list; the engine refuses to
run a mapping while any unconfirmed field remains."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from margin_engine.ingest.mapping import MappingError, load_mapping
from margin_engine.ingest.profiler import draft_mapping, profile_file, render_profile

# A messy EU-style export: semicolon delimiter, windows-1252 encoding (the é),
# comma decimals, euro symbols, parentheses negatives, day-first dotted dates,
# and a short transaction-type code column.
_EU_CSV = (
    "LineKey;InvNo;DocDate;CustNo;CustName;Material;Qty;NetValue;UnitCost;DocType\n"
    "L1;INV001;15.01.2024;C001;Café Supplies GmbH;M100;10;€1.234,56;€617,28;INV\n"
    "L2;INV001;15.01.2024;C001;Café Supplies GmbH;M101;5;€500,00;€250,00;INV\n"
    "L3;INV002;20.01.2024;C002;Nordbau AG;M100;2;(€123,45);€61,72;CRM\n"
    "L4;INV003;28.02.2024;C003;Müller & Sohn;M102;7;€700,07;€350,00;INV\n"
    "L5;INV004;05.03.2024;C001;Café Supplies GmbH;M100;1;€99,99;€50,00;RET\n"
)


@pytest.fixture
def eu_csv(tmp_path: Path) -> Path:
    p = tmp_path / "export.csv"
    p.write_bytes(_EU_CSV.encode("windows-1252"))
    return p


def test_profile_detects_formats(eu_csv: Path) -> None:
    p = profile_file(eu_csv)
    assert p.delimiter == ";"
    assert p.encoding == "windows-1252"
    assert p.decimal_separator == ","
    assert p.negative_parentheses is True
    assert "€" in p.currency_symbols
    assert p.n_rows == 5


def test_profile_columns_and_candidates(eu_csv: Path) -> None:
    p = profile_file(eu_csv)
    by_name = {c.name: c for c in p.columns}
    assert by_name["Qty"].dtype_guess in ("integer", "number")
    assert by_name["NetValue"].dtype_guess == "money"
    assert by_name["DocDate"].dtype_guess == "date"
    assert "%d.%m.%Y" in by_name["DocDate"].date_formats
    # Candidates carry a canonical field, a confidence and a REASON.
    cand = {c.canonical: c for c in by_name["InvNo"].candidates}
    assert "invoice_id" in cand
    assert cand["invoice_id"].confidence >= 0.5
    assert "InvNo" in cand["invoice_id"].reason
    assert any(c.canonical == "customer_id" for c in by_name["CustNo"].candidates)
    assert any(c.canonical == "item_id" for c in by_name["Material"].candidates)
    assert any(c.canonical == "net_amount" for c in by_name["NetValue"].candidates)
    assert any(c.canonical == "unit_cost" for c in by_name["UnitCost"].candidates)


def test_profile_flags_type_codes_and_renders(eu_csv: Path) -> None:
    p = profile_file(eu_csv)
    assert "DocType" in p.txn_type_candidates
    assert p.txn_type_candidates["DocType"] == {"INV": 3, "CRM": 1, "RET": 1}
    text = render_profile(p)
    assert "semicolon" in text
    assert "DocType" in text
    assert "confirmation" in text


def test_ambiguous_dates_reported(tmp_path: Path) -> None:
    csv = (
        "LineKey,InvNo,DocDate,CustNo,Material,Qty,NetValue\n"
        "L1,I1,03/04/2024,C1,M1,1,10.00\n"
        "L2,I2,05/01/2024,C1,M1,1,10.00\n"
    )
    p = tmp_path / "amb.csv"
    p.write_text(csv, encoding="utf-8")
    prof = profile_file(p)
    date_col = next(c for c in prof.columns if c.name == "DocDate")
    # Both day-first and month-first parse: the ambiguity must be visible.
    assert set(date_col.date_formats) >= {"%d/%m/%Y", "%m/%d/%Y"}
    assert "AMBIGUOUS" in render_profile(prof)


def test_draft_mapping_marked_and_refused(eu_csv: Path, tmp_path: Path) -> None:
    p = profile_file(eu_csv)
    text = draft_mapping(p)
    assert "# CONFIRM" in text
    parsed = yaml.safe_load(text)
    assert parsed["unconfirmed_fields"], "draft must list unconfirmed fields"
    assert parsed["input"]["delimiter"] == ";"
    assert parsed["input"]["negative_format"] == "parentheses"
    draft_path = tmp_path / "draft.yaml"
    draft_path.write_text(text, encoding="utf-8")
    with pytest.raises(MappingError, match="unconfirmed"):
        load_mapping(draft_path)


def test_confirmed_draft_loads(eu_csv: Path, tmp_path: Path) -> None:
    """Emptying unconfirmed_fields (the human confirmation act) makes the
    draft a runnable mapping - the emitted skeleton is schema-correct."""
    p = profile_file(eu_csv)
    parsed = yaml.safe_load(draft_mapping(p))
    parsed["unconfirmed_fields"] = []
    confirmed = tmp_path / "confirmed.yaml"
    confirmed.write_text(yaml.safe_dump(parsed), encoding="utf-8")
    mapping = load_mapping(confirmed)
    assert mapping.lines.invoice_id.column == "InvNo"
    assert mapping.lines.invoice_date.format == "%d.%m.%Y"
    assert mapping.lines.unit_cost is not None
    assert mapping.input.delimiter == ";"


def test_suggestions_never_silent_decisions(eu_csv: Path) -> None:
    """Every guessed field in the draft appears in unconfirmed_fields."""
    p = profile_file(eu_csv)
    parsed = yaml.safe_load(draft_mapping(p))
    listed = set(parsed["unconfirmed_fields"])
    for canonical in ("invoice_id", "customer_id", "item_id", "quantity"):
        assert f"lines.{canonical}" in listed
    assert "input.delimiter" in listed
    assert "lines.unit_cost.cost_basis" in listed
