"""Run 6, Phase 85.2: the edge cases the review listed and nothing covered.

Each one is a way a real export, or a real organisation, differs from the
tidy case - and each was previously handled by an assumption nobody had
written down. The tests are the record of which assumption was replaced by
which behaviour.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest
from openpyxl import Workbook

from margin_engine.extended.hierarchy import inherit_parent_contracts
from margin_engine.ingest.loader import (
    _serial_date_hint,
    hidden_rows_seen,
    read_table,
    reset_hidden_rows,
)
from margin_engine.ingest.mapping import InputOptions, MappingError

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# spreadsheets with formulas
# ---------------------------------------------------------------------------


def _wb(path: Path, rows: list[list[object]], *, hide: list[int] | None = None) -> None:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    for idx in hide or []:
        ws.row_dimensions[idx].hidden = True
    wb.save(path)


def test_a_formula_with_no_computed_value_fails_actionably(tmp_path: Path) -> None:
    """openpyxl writes the formula string and no cached result, which is
    exactly what a tool-generated workbook looks like. Reading it as a blank
    would turn =B2*C2 into a missing amount and the engine would then report
    honest arithmetic on dishonest input."""
    path = tmp_path / "lines.xlsx"
    _wb(path, [["KEY", "QTY", "PRICE", "AMOUNT"], ["L1", 2, 5, "=B2*C2"]])
    with pytest.raises(MappingError) as caught:
        read_table(path, InputOptions())
    msg = str(caught.value)
    assert "formula cell" in msg
    assert "=B2*C2" in msg
    assert "save it so the results are stored" in msg


def test_a_workbook_with_real_blanks_still_reads(tmp_path: Path) -> None:
    """The formula check runs only when nulls appear, and a blank cell is not
    a formula: the common case must not become an error."""
    path = tmp_path / "lines.xlsx"
    _wb(path, [["KEY", "NOTE"], ["L1", None], ["L2", "ok"]])
    frame, _ = read_table(path, InputOptions())
    assert frame.height == 2
    assert frame["NOTE"].to_list() == [None, "ok"]


# ---------------------------------------------------------------------------
# hidden rows
# ---------------------------------------------------------------------------


def test_hidden_rows_are_kept_and_counted(tmp_path: Path) -> None:
    """A hidden row is still data. Dropping it would be a decision nobody
    made - a filtered export can hide the very lines under review - so the
    engine keeps them and says how many were hidden."""
    reset_hidden_rows()
    path = tmp_path / "lines.xlsx"
    _wb(
        path,
        [["KEY", "AMT"], ["L1", "10"], ["L2", "20"], ["L3", "30"]],
        hide=[3],  # L2
    )
    frame, _ = read_table(path, InputOptions())
    assert frame.height == 3, "a hidden row was dropped"
    assert "L2" in frame["KEY"].to_list()
    assert hidden_rows_seen()[str(path)] == 1


def test_a_workbook_with_nothing_hidden_records_nothing(tmp_path: Path) -> None:
    reset_hidden_rows()
    path = tmp_path / "lines.xlsx"
    _wb(path, [["KEY", "AMT"], ["L1", "10"]])
    read_table(path, InputOptions())
    assert hidden_rows_seen() == {}


# ---------------------------------------------------------------------------
# date serials
# ---------------------------------------------------------------------------


def test_a_date_serial_is_named_as_one() -> None:
    """45678 is 2025-01-15 in Excel's day count. Telling the user only that
    it "does not match the declared format" is true and useless; the fix is
    in the workbook, not the mapping."""
    hint = _serial_date_hint(["45678", "45679"])
    assert "SERIAL" in hint
    assert "45678" in hint
    assert "Format the column as a date" in hint


def test_ordinary_bad_dates_get_no_serial_hint() -> None:
    """A misread format must not be explained as a serial problem."""
    assert _serial_date_hint(["15/01/2025", "not a date", ""]) == ""
    # Out of the plausible business range: a quantity, not a day count.
    assert _serial_date_hint(["12", "999999"]) == ""


# ---------------------------------------------------------------------------
# parent/child contracts
# ---------------------------------------------------------------------------


def _lines(**over: object) -> pl.DataFrame:
    base = {
        "source_line_key": ["L1", "L2", "L3"],
        "customer_id": ["SHIP-A", "SHIP-B", "SHIP-C"],
        "item_family": ["FAM1", "FAM1", "FAM2"],
        "invoice_date": [dt.date(2025, 6, 1)] * 3,
        "contract_id": [None, None, None],
    }
    base.update(over)
    return pl.DataFrame(base, schema_overrides={"contract_id": pl.String})


def _customers() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "customer_id": ["SHIP-A", "SHIP-B", "SHIP-C"],
            "bill_to_id": ["BILL-1", "BILL-1", "BILL-2"],
            "parent_account_id": ["PARENT-1", "PARENT-1", "PARENT-2"],
        }
    )


def _contracts(**over: object) -> pl.DataFrame:
    base = {
        "contract_id": ["C-NAT"],
        "customer_id": ["PARENT-1"],
        "item_family": ["FAM1"],
        "start_date": [dt.date(2025, 1, 1)],
        "end_date": [dt.date(2025, 12, 31)],
    }
    base.update(over)
    return pl.DataFrame(base)


def test_a_parent_contract_protects_its_children() -> None:
    """A national agreement is signed once, at the parent, while the invoice
    lines carry ship-to ids and no contract number. Before Phase 85 every
    child line was freely priceable - eligible for increases on exactly the
    customers a contract forbids repricing."""
    out, notes = inherit_parent_contracts(_lines(), _contracts(), _customers())
    got = dict(zip(out["source_line_key"], out["contract_id"], strict=True))
    assert got["L1"] == "C-NAT"  # SHIP-A, under PARENT-1, family FAM1
    assert got["L2"] == "C-NAT"  # SHIP-B, same parent
    assert got["L3"] is None  # PARENT-2, and a different family
    assert out["contract_inherited"].to_list() == [True, True, False]
    assert any("inherited contract protection" in n for n in notes)


def test_a_lines_own_contract_always_wins() -> None:
    out, _ = inherit_parent_contracts(
        _lines(contract_id=["C-OWN", None, None]), _contracts(), _customers()
    )
    got = dict(zip(out["source_line_key"], out["contract_id"], strict=True))
    assert got["L1"] == "C-OWN"
    assert out["contract_inherited"].to_list() == [False, True, False]


def test_a_contract_outside_its_dates_does_not_protect() -> None:
    expired = _contracts(start_date=[dt.date(2024, 1, 1)], end_date=[dt.date(2024, 12, 31)])
    out, notes = inherit_parent_contracts(_lines(), expired, _customers())
    assert out["contract_id"].to_list() == [None, None, None]
    assert not any("inherited" in n for n in notes)


def test_an_open_ended_contract_covers_everything_after_its_start() -> None:
    open_ended = _contracts(end_date=[None])
    out, _ = inherit_parent_contracts(_lines(), open_ended, _customers())
    assert out["contract_id"].to_list()[:2] == ["C-NAT", "C-NAT"]


def test_a_contract_with_no_family_covers_every_family() -> None:
    allfam = _contracts(item_family=[None])
    out, _ = inherit_parent_contracts(_lines(), allfam, _customers())
    # SHIP-C is under PARENT-2, so still untouched; SHIP-A/B now covered.
    assert out["contract_id"].to_list() == ["C-NAT", "C-NAT", None]


def test_a_contract_named_at_the_bill_to_reaches_the_ship_tos() -> None:
    at_bill_to = _contracts(customer_id=["BILL-1"])
    out, _ = inherit_parent_contracts(_lines(), at_bill_to, _customers())
    assert out["contract_id"].to_list()[:2] == ["C-NAT", "C-NAT"]


def test_several_covering_contracts_pick_deterministically() -> None:
    two = pl.concat(
        [_contracts(), _contracts(contract_id=["C-AAA"], customer_id=["BILL-1"])],
        how="vertical_relaxed",
    )
    first = inherit_parent_contracts(_lines(), two, _customers())[0]["contract_id"].to_list()
    second = inherit_parent_contracts(_lines(), two, _customers())[0]["contract_id"].to_list()
    assert first == second
    assert first[0] == "C-AAA"  # sorted, so the choice is stated not accidental


def test_no_contracts_table_changes_nothing() -> None:
    out, notes = inherit_parent_contracts(_lines(), None, _customers())
    assert out["contract_id"].to_list() == [None, None, None]
    assert notes == []


# ---------------------------------------------------------------------------
# FX: analytical vs book translation
# ---------------------------------------------------------------------------


def test_a_converted_ledger_states_the_translation_policy() -> None:
    """A GL translated at closing or average rates differs from the engine's
    per-invoice-period conversion by policy alone. Saying so first stops a
    reviewer hunting for a missing dollar that is not missing."""
    from margin_engine.config import Rules
    from margin_engine.report.tieout import run_tieout

    ledger = pl.DataFrame(
        {
            "source_line_key": ["L1"],
            "invoice_date": [dt.date(2025, 1, 15)],
            "transaction_type": ["sale"],
            "net_amount_cents": [10_000],
            "net_amount_cents_src": [8_000],  # the marker: this ran through FX
            "product_cost_cents": [6_000],
            "quantity_norm": [1.0],
        }
    )
    gl = pl.DataFrame({"month": ["2025-01"], "net_sales_cents": [10_000], "cogs_cents": [6_000]})
    res = run_tieout(ledger, gl, Rules())
    assert any("translation policy alone" in n for n in res.notes)
    assert any("invoice period's rate" in n for n in res.notes)


def test_a_single_currency_ledger_says_nothing_about_translation() -> None:
    from margin_engine.config import Rules
    from margin_engine.report.tieout import run_tieout

    ledger = pl.DataFrame(
        {
            "source_line_key": ["L1"],
            "invoice_date": [dt.date(2025, 1, 15)],
            "transaction_type": ["sale"],
            "net_amount_cents": [10_000],
            "product_cost_cents": [6_000],
            "quantity_norm": [1.0],
        }
    )
    gl = pl.DataFrame({"month": ["2025-01"], "net_sales_cents": [10_000], "cogs_cents": [6_000]})
    res = run_tieout(ledger, gl, Rules())
    assert not any("translation policy" in n for n in res.notes)


# ---------------------------------------------------------------------------
# the run says what it saw
# ---------------------------------------------------------------------------


def test_hiding_rows_changes_the_notes_and_nothing_else(tmp_path: Path) -> None:
    """End to end: the same workbook with and without hidden rows produces
    the same ledger and the same money, and only the hidden version says so.

    Comparing against the CSV row count would prove nothing - classification
    legitimately drops verified duplicates - so the comparison is between two
    runs that differ ONLY in which rows are hidden.
    """
    import shutil

    import yaml

    from margin_engine.config import load_rules
    from margin_engine.ledger.pipeline import run_pipeline

    sample = REPO / "data" / "sample" / "erp"
    rules = load_rules(REPO / "config" / "rules.yaml")
    raw = yaml.safe_load((REPO / "config" / "mappings" / "synth_erp.yaml").read_text("utf-8"))
    raw["lines_file"] = "invoice_lines.xlsx"
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(yaml.safe_dump(raw), encoding="utf-8")

    def world_with(hidden: list[int], name: str) -> Path:
        world = tmp_path / name
        shutil.copytree(sample, world)
        csv = pl.read_csv(world / "invoice_lines.csv", infer_schema=False)
        wb = Workbook()
        ws = wb.active
        ws.append(list(csv.columns))
        for row in csv.iter_rows():
            ws.append(["" if v is None else str(v) for v in row])
        for idx in hidden:
            ws.row_dimensions[idx].hidden = True
        (world / "invoice_lines.csv").unlink()
        wb.save(world / "invoice_lines.xlsx")
        return world

    plain = run_pipeline(world_with([], "plain"), mapping, rules)
    filtered = run_pipeline(world_with([4, 9], "filtered"), mapping, rules)

    assert filtered.ledger.height == plain.ledger.height
    assert int(filtered.ledger["net_amount_cents"].sum()) == int(
        plain.ledger["net_amount_cents"].sum()
    )
    note = next((n for n in filtered.notes if "hidden in the workbook" in n), None)
    assert note is not None, filtered.notes
    assert "2 row(s)" in note
    assert "INCLUDED in every total" in note
    assert not any("hidden in the workbook" in n for n in plain.notes)
