"""Tiny fixture-world builder: writes ERP-format CSVs the synth mapping reads.

Every helper takes explicit dollar strings so tests stay hand-checkable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LINE_COLUMNS = [
    "LINE_KEY",
    "INV_NO",
    "INV_DT",
    "ORD_NO",
    "CUST_NO",
    "ITEM_NO",
    "QTY_SHP",
    "SELL_UOM",
    "UNIT_PRC",
    "EXT_NET",
    "LIST_PRC",
    "DISC_AMT",
    "UNIT_CST",
    "TXN_CD",
    "REASON_CD",
    "ORIG_INV",
    "REP_ID",
    "CHNL",
    "FRT_CONC",
    "RESTOCK_FEE",
    "RTN_FRT_BILLED",
    "REB_AMT",
    "REB_PGM",
    "CONTRACT_NO",
    "KIT_ID",
    "CURR",
]

LINE_DEFAULTS: dict[str, str] = {
    "SELL_UOM": "EA",
    "TXN_CD": "S",
    "REASON_CD": "",
    "ORIG_INV": "",
    "REP_ID": "REP1",
    "CHNL": "field",
    "FRT_CONC": "0.00",
    "RESTOCK_FEE": "0.00",
    "RTN_FRT_BILLED": "0.00",
    "REB_AMT": "0.00",
    "REB_PGM": "",
    "CONTRACT_NO": "",
    "KIT_ID": "",
    "CURR": "USD",
    "DISC_AMT": "0.00",
    "LIST_PRC": "",
    "UNIT_CST": "",
    "UNIT_PRC": "0",
    "ORD_NO": "ORD1",
    "INV_DT": "03/15/2024",
}


@dataclass
class FixtureWorld:
    tmp: Path
    lines: list[dict[str, str]] = field(default_factory=list)
    price_list: list[dict[str, str]] = field(default_factory=list)
    uom_conversions: list[dict[str, str]] = field(default_factory=list)
    items: list[dict[str, str]] = field(default_factory=list)
    order_charges: list[dict[str, str]] = field(default_factory=list)
    freight_log: list[dict[str, str]] = field(default_factory=list)
    rebate_settlements: list[dict[str, str]] = field(default_factory=list)
    rebate_programs: list[dict[str, str]] = field(default_factory=list)
    rebate_program_tiers: list[dict[str, str]] = field(default_factory=list)
    tiered_settlements: list[dict[str, str]] = field(default_factory=list)
    return_freight_log: list[dict[str, str]] = field(default_factory=list)
    fx_rates: list[dict[str, str]] = field(default_factory=list)
    vendor_rebate_programs: list[dict[str, str]] = field(default_factory=list)
    landed_cost_components: list[dict[str, str]] = field(default_factory=list)
    receipts: list[dict[str, str]] = field(default_factory=list)

    def add_line(self, **kw: Any) -> None:
        row = dict(LINE_DEFAULTS)
        row["LINE_KEY"] = f"L{len(self.lines) + 1}"
        row.update({k: str(v) for k, v in kw.items()})
        self.lines.append(row)

    def add_item(
        self,
        item: str,
        base_uom: str = "EA",
        weight: str = "1.0",
        supplier: str = "SUPA",
        service: bool = False,
    ) -> None:
        self.items.append(
            {
                "ITEM_NO": item,
                "ITEM_DESC": f"Fixture item {item}",
                "FAMILY": "FAM1",
                "CATEGORY": "CAT1",
                "CAT_NAME": "Fixture category",
                "SUPPLIER": supplier,
                "SUPP_NAME": f"Fixture supplier {supplier}",
                "ITEM_ROLE": "core",
                "SVC_FLAG": "1" if service else "0",
                "BASE_UOM": base_uom,
                "WEIGHT_LB": weight,
                "CUBE_FT": "0.1",
            }
        )

    def add_list_price(self, item: str, eff_dt: str, price: str) -> None:
        self.price_list.append({"ITEM_NO": item, "EFF_DT": eff_dt, "LIST_PRC": price})

    def write(self) -> Path:
        def dump(name: str, rows: list[dict[str, str]], columns: list[str] | None = None) -> None:
            if not rows:
                return
            cols = columns or list(rows[0].keys())
            out = [",".join(cols)]
            out.extend(",".join(r.get(c, "") for c in cols) for r in rows)
            (self.tmp / name).write_text("\n".join(out) + "\n", encoding="utf-8")

        self.tmp.mkdir(parents=True, exist_ok=True)
        dump("invoice_lines.csv", self.lines, LINE_COLUMNS)
        dump("price_list.csv", self.price_list)
        dump("uom_conversions.csv", self.uom_conversions)
        dump("items.csv", self.items)
        dump("order_charges.csv", self.order_charges)
        dump("freight_log.csv", self.freight_log)
        dump("rebate_settlements.csv", self.rebate_settlements)
        dump("rebate_programs.csv", self.rebate_programs)
        dump("rebate_program_tiers.csv", self.rebate_program_tiers)
        dump("tiered_settlements.csv", self.tiered_settlements)
        dump("return_freight_log.csv", self.return_freight_log)
        dump("fx_rates.csv", self.fx_rates)
        dump("vendor_rebate_programs.csv", self.vendor_rebate_programs)
        dump("landed_cost_components.csv", self.landed_cost_components)
        dump("receipts.csv", self.receipts)
        return self.tmp
