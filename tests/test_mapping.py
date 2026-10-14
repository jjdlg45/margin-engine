"""Phase 2 mapping tests: different column names/layouts give identical
canonical output; missing or ambiguous semantics fail actionably; malformed
input fails with a useful message. The engine never guesses.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
import yaml

from margin_engine.ingest.loader import load_lines
from margin_engine.ingest.mapping import MappingError, load_mapping
from margin_engine.money import MoneyParseError

BASE_MAPPING = {
    "source_system": "test_a",
    "lines_file": "lines.csv",
    "lines": {
        "source_line_key": {"column": "KEY"},
        "invoice_id": {"column": "INV"},
        "invoice_date": {"column": "DT", "format": "%Y-%m-%d"},
        "customer_id": {"column": "CUST"},
        "item_id": {"column": "ITEM"},
        "quantity": {"column": "QTY"},
        "uom": {"column": "UOM"},
        "net_amount": {
            "column": "NET",
            "currency": "USD",
            "sign_convention": "natural",
            "discounts_embedded": True,
        },
        "unit_cost": {
            "column": "COST",
            "currency": "USD",
            "cost_basis": "average",
            "includes_inbound_freight": False,
            "includes_duty": False,
            "includes_vendor_rebates": False,
            "per_uom": "line_uom",
        },
        "transaction_type": {"column": "TYPE", "code_map": {"SALE": "sale", "RTN": "return"}},
    },
}

CSV_A = """KEY,INV,DT,CUST,ITEM,QTY,UOM,NET,COST,TYPE
k1,i1,2024-02-03,c1,x1,5,EA,250.00,30.0000,SALE
k2,i2,2024-02-04,c1,x1,-2,EA,-100.00,30.0000,RTN
"""

# Same canonical data: reordered columns, different names, different date format,
# ERP-style codes.
CSV_B = """TxnKind,Amount,DocDate,Qty,Cost,Sku,Account,Doc,RowId,Unit
S,250.00,02/03/2024,5,30.0000,x1,c1,i1,k1,EA
X,-100.00,02/04/2024,-2,30.0000,x1,c1,i2,k2,EA
"""

MAPPING_B = {
    "source_system": "test_b",
    "lines_file": "lines.csv",
    "lines": {
        "source_line_key": {"column": "RowId"},
        "invoice_id": {"column": "Doc"},
        "invoice_date": {"column": "DocDate", "format": "%m/%d/%Y"},
        "customer_id": {"column": "Account"},
        "item_id": {"column": "Sku"},
        "quantity": {"column": "Qty"},
        "uom": {"column": "Unit"},
        "net_amount": {
            "column": "Amount",
            "currency": "USD",
            "sign_convention": "natural",
            "discounts_embedded": True,
        },
        "unit_cost": {
            "column": "Cost",
            "currency": "USD",
            "cost_basis": "average",
            "includes_inbound_freight": False,
            "includes_duty": False,
            "includes_vendor_rebates": False,
            "per_uom": "line_uom",
        },
        "transaction_type": {"column": "TxnKind", "code_map": {"S": "sale", "X": "return"}},
    },
}


def _write_mapping(tmp: Path, mapping: dict, name: str = "m.yaml") -> Path:
    p = tmp / name
    p.write_text(yaml.safe_dump(mapping), encoding="utf-8")
    return p


def _load(tmp: Path, mapping: dict, csv: str) -> pl.DataFrame:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "lines.csv").write_text(csv, encoding="utf-8")
    return load_lines(tmp, load_mapping(_write_mapping(tmp, mapping)))


def test_equivalent_mappings_identical_canonical_output(tmp_path: Path) -> None:
    a = _load(tmp_path / "a", BASE_MAPPING, CSV_A)
    b = _load(tmp_path / "b", MAPPING_B, CSV_B)
    cols = [
        "source_line_key",
        "invoice_id",
        "invoice_date",
        "customer_id",
        "item_id",
        "quantity",
        "uom",
        "net_amount_cents",
        "unit_cost_scaled",
        "transaction_type",
    ]
    assert a.select(cols).equals(b.select(cols))


def test_missing_cost_basis_rejected(tmp_path: Path) -> None:
    bad = yaml.safe_load(yaml.safe_dump(BASE_MAPPING))
    del bad["lines"]["unit_cost"]["cost_basis"]
    with pytest.raises(MappingError, match="cost_basis"):
        _load(tmp_path, bad, CSV_A)


def test_missing_sign_convention_rejected(tmp_path: Path) -> None:
    bad = yaml.safe_load(yaml.safe_dump(BASE_MAPPING))
    del bad["lines"]["net_amount"]["sign_convention"]
    with pytest.raises(MappingError, match="sign_convention"):
        _load(tmp_path, bad, CSV_A)


def test_no_price_and_no_net_rejected(tmp_path: Path) -> None:
    bad = yaml.safe_load(yaml.safe_dump(BASE_MAPPING))
    del bad["lines"]["net_amount"]
    with pytest.raises(MappingError, match="unit_price or net_amount"):
        _load(tmp_path, bad, CSV_A)


def test_no_transaction_type_rejected(tmp_path: Path) -> None:
    bad = yaml.safe_load(yaml.safe_dump(BASE_MAPPING))
    del bad["lines"]["transaction_type"]
    with pytest.raises(MappingError, match="transaction_type"):
        _load(tmp_path, bad, CSV_A)


def test_unknown_transaction_code_rejected(tmp_path: Path) -> None:
    csv = CSV_A + "k3,i3,2024-02-05,c1,x1,1,EA,10.00,5.0000,MYSTERY\n"
    with pytest.raises(MappingError, match="MYSTERY"):
        _load(tmp_path, BASE_MAPPING, csv)


def test_missing_mapped_column_lists_available(tmp_path: Path) -> None:
    csv = CSV_A.replace("NET", "TOTAL")
    with pytest.raises(MappingError, match="NET"):
        _load(tmp_path, BASE_MAPPING, csv)


def test_bad_date_format_reported(tmp_path: Path) -> None:
    csv = CSV_A.replace("2024-02-03", "03/02/2024")
    with pytest.raises(MappingError, match="invoice_date"):
        _load(tmp_path, BASE_MAPPING, csv)


def test_unparseable_money_reported(tmp_path: Path) -> None:
    csv = CSV_A.replace("250.00", "2S0.00")
    with pytest.raises(MoneyParseError, match="NET"):
        _load(tmp_path, BASE_MAPPING, csv)


def test_too_many_decimals_refused(tmp_path: Path) -> None:
    csv = CSV_A.replace("250.00", "250.000001")
    with pytest.raises(MoneyParseError, match="decimal places"):
        _load(tmp_path, BASE_MAPPING, csv)


def test_comma_decimal_separator(tmp_path: Path) -> None:
    mapping = yaml.safe_load(yaml.safe_dump(BASE_MAPPING))
    mapping["decimal_separator"] = ","
    csv = (
        CSV_A.replace("250.00", '"1.250,50"')
        .replace("30.0000", '"30,0000"')
        .replace("-100.00", '"-100,00"')
    )
    df = _load(tmp_path, mapping, csv)
    assert df["net_amount_cents"].to_list() == [125_050, -10_000]


def test_unknown_reference_table_rejected(tmp_path: Path) -> None:
    bad = yaml.safe_load(yaml.safe_dump(BASE_MAPPING))
    bad["reference_tables"] = {"mystery_table": {"file": "x.csv", "columns": {"a": "A"}}}
    with pytest.raises(MappingError, match="mystery_table"):
        _write_mapping(tmp_path, bad)
        load_mapping(tmp_path / "m.yaml")


def test_unit_price_not_authoritative_without_net_rejected(tmp_path: Path) -> None:
    bad = yaml.safe_load(yaml.safe_dump(BASE_MAPPING))
    del bad["lines"]["net_amount"]
    bad["lines"]["unit_price"] = {"column": "PRC", "currency": "USD", "authoritative": False}
    with pytest.raises(MappingError, match="authoritative"):
        _load(tmp_path, bad, CSV_A)


def test_net_computed_from_authoritative_unit_price(tmp_path: Path) -> None:
    mapping = yaml.safe_load(yaml.safe_dump(BASE_MAPPING))
    del mapping["lines"]["net_amount"]
    mapping["lines"]["unit_price"] = {
        "column": "PRC",
        "currency": "USD",
        "authoritative": True,
    }
    csv = """KEY,INV,DT,CUST,ITEM,QTY,UOM,PRC,COST,TYPE
k1,i1,2024-02-03,c1,x1,7,EA,19.99,10.0000,SALE
"""
    df = _load(tmp_path, mapping, csv)
    assert df["net_amount_cents"].to_list() == [13_993]  # 7 x 19.99


def test_cli_exits_1_with_clean_message_on_mapping_error(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from margin_engine.cli import app

    csv = CSV_A.replace("250.00", "2S0.00")  # unparseable money on line k1
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "lines.csv").write_text(csv, encoding="utf-8")
    mp = _write_mapping(tmp_path, BASE_MAPPING)
    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--data", str(tmp_path), "--mapping", str(mp), "--out", str(tmp_path / "o")]
    )
    assert result.exit_code == 1
    combined = (result.output or "") + str(result.exception or "")
    assert "ERROR" in combined or "unparseable" in combined
    assert "Traceback" not in (result.output or "")


def test_money_error_lists_offending_keys(tmp_path: Path) -> None:
    csv = CSV_A.replace("250.00", "2S0.00")
    with pytest.raises(MoneyParseError, match="k1"):
        _load(tmp_path, BASE_MAPPING, csv)


def test_date_error_lists_offending_keys(tmp_path: Path) -> None:
    csv = CSV_A.replace("2024-02-03", "03/02/2024")
    with pytest.raises(MappingError, match="k1"):
        _load(tmp_path, BASE_MAPPING, csv)
