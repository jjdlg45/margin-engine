"""Phase 64 exit gate (Run 6): twenty hand-picked cases traced from the source
document through the line waterfall, the rebate subledger and the COGS effect
into the monthly GL bridge - with every difference explained.

Every number below is hand-calculable from the fixture lines. One GL
difference is planted DELIBERATELY (the books carry full cost for a line
whose cost the export does not have) and the test asserts the variance equals
that explanation to the cent - flagged, never hidden.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from margin_engine.config import Rules
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline
from margin_engine.ledger.rebate_subledger import SubledgerResult, build_rebate_subledger
from margin_engine.report.tieout import (
    STATUS_EXACT,
    STATUS_OUTSIDE,
    STATUS_WITHIN,
    TieoutResult,
    run_tieout,
)
from tests.fixtures.builder import FixtureWorld

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"

RULES = Rules.model_validate(
    {"transactions": {"supplier_claim_reasons": ["supplier_damage_recovery"]}}
)


def _bridge_mapping(tmp_path: Path) -> Path:
    """Synth mapping extended with the tax field, dispositions and charge
    declarations used by the twenty cases."""
    raw = yaml.safe_load(MAPPING.read_text(encoding="utf-8"))
    raw["lines"]["tax_amount"] = {
        "column": "TAX_AMT",
        "currency": "line_currency",
        "sign_convention": "natural",
        "embedded_in_net": True,
    }
    raw["lines"]["return_disposition"] = {"column": "RTN_DISP"}
    raw["return_disposition_map"] = {"OK": "resaleable", "DMG": "damaged"}
    raw["charge_declarations"] = {
        "FRT_OUT": {
            "economic_owner": "customer",
            "direction": "revenue_increase",
            "pool": "pass_through_revenue",
            "amount_basis": "per order",
        },
        "DEPOSIT": {
            "economic_owner": "tax_authority",
            "direction": "revenue_increase",
            "pool": "excluded_liability",
            "amount_basis": "per order",
        },
    }
    out = tmp_path / "bridge_mapping.yaml"
    out.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return out


def _world(tmp_path: Path) -> FixtureWorld:
    w = FixtureWorld(tmp_path)
    for item, supplier in (("X1", "SUPB"), ("X2", "SUPA"), ("SVC", "SUPB")):
        w.add_item(item, supplier=supplier, service=(item == "SVC"))
    w.add_list_price("X1", "01/01/2024", "100.00")
    w.add_list_price("X2", "01/01/2024", "100.00")
    w.add_list_price("SVC", "01/01/2024", "30.00")

    def sale(inv: str, cust: str, dt: str, net: str, **kw: Any) -> None:
        base: dict[str, Any] = {
            "INV_NO": inv,
            "CUST_NO": cust,
            "ITEM_NO": "X1",
            "QTY_SHP": 10,
            "UNIT_PRC": "95.00",
            "EXT_NET": net,
            "LIST_PRC": "100.00",
            "UNIT_CST": "60.0000",
            "INV_DT": dt,
            "ORD_NO": f"ORD_{inv}",
        }
        base.update(kw)
        w.add_line(**base)

    # --- January: cases 1-12 ------------------------------------------------
    sale("INV01", "C1", "01/10/2024", "950.00")  # 1 plain sale
    sale("INV02", "C1", "01/10/2024", "950.00", DISC_AMT="50.00")  # 2 discount
    sale("INV03", "C1", "01/11/2024", "950.00", REB_AMT="19.00", REB_PGM="RPL")  # 3 line rebate
    sale("INV04", "C1", "01/12/2024", "950.00", FRT_CONC="12.00")  # 4 concession
    sale("INV05", "C2", "01/15/2024", "600.00")  # 5 settlement-program sale (Jan)
    sale("INV09", "C1", "01/16/2024", "950.00")  # 9 billed charge order (below)
    sale("INV10", "C1", "01/17/2024", "950.00")  # 10 excluded deposit charge
    sale("INV11", "C1", "01/18/2024", "950.00", TAX_AMT="50.00")  # 11 embedded tax
    sale("INV17", "C1", "01/19/2024", "950.00", ITEM_NO="X2")  # 17 vendor rebate
    sale("INV18", "C1", "01/20/2024", "550.00", UNIT_PRC="55.00", UNIT_CST="")  # 18 missing cost
    sale(
        "INV19", "C1", "01/21/2024", "300.00", ITEM_NO="SVC", UNIT_PRC="30.00", UNIT_CST="0.0000"
    )  # 19 zero cost
    sale("INV20", "C1", "01/22/2024", "25.00", QTY_SHP=0, UNIT_PRC="0")  # 20 zero-qty fee

    # --- February: cases 6-7, 12-13 ------------------------------------------
    sale("INV06", "C2", "02/10/2024", "400.00")  # 6 settlement-program sale (Feb)
    sale("INV07", "C3", "02/12/2024", "600.00")  # 7 tiered sale, tier 1
    w.add_line(  # 12 return, resaleable: cost recovered
        INV_NO="RET12",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-2,
        UNIT_PRC="95.00",
        EXT_NET="-190.00",
        TXN_CD="R",
        ORIG_INV="INV01",
        UNIT_CST="60.0000",
        RTN_DISP="OK",
        INV_DT="02/15/2024",
    )
    w.add_line(  # 13 return, damaged: no cost recovery
        INV_NO="RET13",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-2,
        UNIT_PRC="95.00",
        EXT_NET="-190.00",
        TXN_CD="R",
        ORIG_INV="INV02",
        UNIT_CST="60.0000",
        RTN_DISP="DMG",
        INV_DT="02/15/2024",
    )

    # --- March: case 8 ---------------------------------------------------------
    sale("INV08", "C3", "03/12/2024", "600.00")  # 8 tiered sale, crosses the tier

    # --- credits: cases 14-16 (never in GL net sales; pocket/cost effects) ----
    for inv, reason, net, case in (
        ("CM14", "price_correction", "-50.00", "price credit"),
        ("CM15", "damage", "-80.00", "customer claim"),
        ("CM16", "supplier_damage_recovery", "50.00", "supplier recovery"),
    ):
        w.add_line(
            INV_NO=inv,
            CUST_NO="C1",
            ITEM_NO="X1",
            QTY_SHP=0,
            UNIT_PRC="0",
            EXT_NET=net,
            TXN_CD="C",
            REASON_CD=reason,
            ORIG_INV="INV01",
            INV_DT="01/25/2024",
        )
        del case

    w.order_charges = [
        {"ORD_NO": "ORD_INV09", "CHG_CD": "FRT_OUT", "AMT": "20.00"},  # case 9
        {"ORD_NO": "ORD_INV10", "CHG_CD": "DEPOSIT", "AMT": "30.00"},  # case 10
    ]
    w.rebate_programs = [
        {"PGM_ID": "RPL", "CUST_NO": "C1", "KIND": "line_accrual", "RATE_PCT": "2.0"},
        {"PGM_ID": "RPS", "CUST_NO": "C2", "KIND": "settlement", "RATE_PCT": "2.0"},
        {"PGM_ID": "TIER01", "CUST_NO": "C3", "KIND": "tiered", "RATE_PCT": "0.0"},
    ]
    w.rebate_settlements = [  # 2% x (600 + 400) settled for Q1, exact
        {"CUST_NO": "C2", "PERIOD": "2024-Q1", "PGM_ID": "RPS", "AMT": "20.00"}
    ]
    w.rebate_program_tiers = [
        {"PGM_ID": "TIER01", "MIN_AMT": "0.00", "RATE_PCT": "1.0"},
        {"PGM_ID": "TIER01", "MIN_AMT": "1000.00", "RATE_PCT": "2.0"},
    ]
    w.tiered_settlements = [  # 2% x 1200 retrospective
        {"CUST_NO": "C3", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "24.00"}
    ]
    w.vendor_rebate_programs = [{"SUPPLIER": "SUPA", "RATE_PCT": "5.0"}]  # case 17
    return w


# Hand-computed engine period totals (cents). January net sales: seven plain
# 950.00 sales + 600.00 + the tax-free 900.00 + 550.00 + 300.00 + 25.00 fee.
JAN_NET = 7 * 95_000 + 60_000 + 90_000 + 55_000 + 30_000 + 2_500  # 902_500
# January COGS: eight cost-known sales at 600.00 each (cases 1-5, 9-11's tax
# sale, and the billed/deposit orders) + the vendor-netted 570.00; zero-cost
# and zero-quantity lines add 0 and the missing-cost line contributes nothing.
JAN_COGS = 8 * 60_000 + 57_000
FEB_NET = 40_000 + 60_000 - 19_000 - 19_000  # 62_000
# February COGS: two sales at 600.00; the resaleable return RECOVERS 120.00
# (negative cost), the damaged one recovers nothing.
FEB_COGS = 60_000 + 60_000 - 12_000
MAR_NET = 60_000
MAR_COGS = 60_000
# The books carry FULL cost, including the 330.00 the export never had for the
# missing-cost line - the deliberate, explained GL difference.
L18_TRUE_COST = 33_000


@pytest.fixture(scope="module")
def bridge(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[PipelineResult, SubledgerResult, TieoutResult]:
    tmp = tmp_path_factory.mktemp("bridge")
    world = _world(tmp)
    res = run_pipeline(world.write(), _bridge_mapping(tmp), RULES)
    assert res.reconciliation.all_pass
    sub = build_rebate_subledger(
        res.ledger,
        res.reference["rebate_programs"],
        res.reference["rebate_settlements"],
        res.tiered_monthly,
        res.tiered_summary,
    )
    gl = pl.DataFrame(
        {
            "month": ["2024-01", "2024-02", "2024-03"],
            "net_sales_cents": [JAN_NET, FEB_NET + 80, MAR_NET],  # Feb off 80c
            "cogs_cents": [JAN_COGS + L18_TRUE_COST, FEB_COGS, MAR_COGS],
        }
    )
    tie = run_tieout(res.ledger, gl, RULES)
    return res, sub, tie


def _row(res: PipelineResult, key: str) -> dict[str, Any]:
    return res.ledger.filter(pl.col("source_line_key") == key).to_dicts()[0]


def test_cases_1_to_4_waterfall_to_the_cent(bridge: tuple) -> None:
    res = bridge[0]
    plain = _row(res, "L1")
    assert plain["invoice_revenue_cents"] == 95_000
    # The attributed price credit (case 14) reduces pocket; the claims (cases
    # 15-16) are cost-side and never touch revenue.
    assert plain["pocket_revenue_cents"] == 95_000 - 5_000
    disc = _row(res, "L2")
    assert disc["discount_cents"] == 5_000
    assert disc["pocket_revenue_cents"] == 95_000
    reb = _row(res, "L3")
    assert reb["rebate_actual_cents"] == 1_900
    assert reb["pocket_revenue_cents"] == 95_000 - 1_900
    conc = _row(res, "L4")
    assert conc["freight_concession_cents"] == 1_200
    assert conc["pocket_revenue_cents"] == 95_000 - 1_200


def test_cases_5_6_settlement_program_subledger(bridge: tuple) -> None:
    res, sub = bridge[0], bridge[1]
    # Settlement allocation lands on C2's lines: 20.00 over 600/400 nets.
    jan = _row(res, "L5")
    feb = _row(res, "L13")
    assert jan["rebate_settlement_alloc_cents"] == 1_200
    assert feb["rebate_settlement_alloc_cents"] == 800
    # The subledger explains it: estimates 12.00 + 8.00, the settlement row
    # applied once at the quarter's closing month, no true-up, balance zero.
    bal = sub.balances.filter(pl.col("customer_id") == "C2").sort("period")
    assert bal["period"].to_list() == ["2024-01", "2024-02", "2024-03"]
    assert bal["estimates_cents"].to_list() == [1_200, 800, 0]
    assert bal["settlements_cents"].to_list() == [0, 0, 2_000]
    assert bal["closing_cents"].to_list() == [1_200, 2_000, 0]
    assert bal["revisions_cents"].to_list() == [0, 0, 0]  # settled exactly


def test_cases_7_8_tiered_lifecycle(bridge: tuple) -> None:
    res, sub = bridge[0], bridge[1]
    feb = _row(res, "L14")
    mar = _row(res, "L17")
    assert feb["tiered_rate_bp"] == 100
    assert mar["tiered_rate_bp"] == 200
    assert feb["tiered_actual_alloc_cents"] + mar["tiered_actual_alloc_cents"] == 2_400
    c3 = sub.balances.filter(pl.col("customer_id") == "C3").sort("period")
    # Feb: estimate 6.00. Mar: estimate 12.00 + retrospective revision 6.00,
    # settlement 24.00 closes the year exactly.
    assert c3["estimates_cents"].to_list() == [600, 1_200]
    assert c3["revisions_cents"].to_list() == [0, 600]
    assert c3["closing_cents"].to_list() == [600, 0]


def test_cases_9_10_11_charges_and_tax(bridge: tuple) -> None:
    res = bridge[0]
    billed = _row(res, "L6")
    assert billed["billed_charge_alloc_cents"] == 2_000
    assert billed["invoice_revenue_cents"] == 97_000
    deposit_line = _row(res, "L7")
    assert deposit_line["billed_charge_alloc_cents"] == 0  # excluded liability
    assert res.reconciliation.residuals["billed_charges_excluded_liability_cents"] == 3_000
    taxed = _row(res, "L8")
    assert taxed["tax_collected_cents"] == 5_000
    assert taxed["net_product_cents"] == 90_000
    assert taxed["invoice_revenue_cents"] == 90_000


def test_cases_12_13_return_dispositions(bridge: tuple) -> None:
    res = bridge[0]
    ok = _row(res, "L15")
    assert ok["return_cost_recovered"] is True
    assert ok["product_cost_cents"] == -12_000
    damaged = res.ledger.filter(pl.col("invoice_id") == "RET13").to_dicts()[0]
    assert damaged["return_cost_recovered"] is False
    assert damaged["product_cost_cents"] == 0


def test_cases_14_to_16_credits(bridge: tuple) -> None:
    res = bridge[0]
    orig = _row(res, "L1")
    # Price credit 50.00 attributed to INV01; claim 80.00 and supplier
    # recovery 50.00 land in cost-to-serve on the same lines.
    assert orig["price_credit_alloc_cents"] == -5_000
    assert orig["claims_cost_alloc_cents"] == 3_000
    assert orig["cost_to_serve_cents"] == 3_000


def test_cases_17_to_20_cost_variants(bridge: tuple) -> None:
    res = bridge[0]
    vend = _row(res, "L9")
    assert vend["vendor_rebate_cents"] == 3_000
    assert vend["product_cost_cents"] == 57_000
    missing = _row(res, "L10")
    assert missing["product_cost_cents"] is None
    assert missing["flag_missing_cost"] is True
    zero = _row(res, "L11")
    assert zero["product_cost_cents"] == 0
    assert zero["flag_zero_cost"] is True
    fee = _row(res, "L12")
    assert fee["flag_zero_quantity"] is True
    assert fee["net_amount_cents"] == 2_500


def test_monthly_gl_bridge_every_difference_explained(bridge: tuple) -> None:
    tie = bridge[2]
    rows = {(r["month"], r["measure"]): r for r in tie.table.to_dicts()}
    # January net sales: exact to the cent.
    assert rows[("2024-01", "net_sales")]["status"] == STATUS_EXACT
    assert rows[("2024-01", "net_sales")]["engine_cents"] == JAN_NET
    # January COGS: the books carry the missing-cost line's full 330.00 - the
    # variance equals that explanation EXACTLY and is flagged, never hidden.
    jan_cogs = rows[("2024-01", "cogs")]
    assert jan_cogs["variance_cents"] == -L18_TRUE_COST
    assert jan_cogs["status"] == STATUS_OUTSIDE
    assert any("standing reconciling item" in i for i in tie.unresolved_items)
    # February net sales: off by 80 cents, inside the cents tolerance.
    feb_net = rows[("2024-02", "net_sales")]
    assert feb_net["variance_cents"] == -80
    assert feb_net["status"] == STATUS_WITHIN
    # Everything else ties exactly.
    assert rows[("2024-03", "net_sales")]["status"] == STATUS_EXACT
    assert rows[("2024-03", "cogs")]["status"] == STATUS_EXACT
    assert rows[("2024-02", "cogs")]["status"] == STATUS_EXACT
    assert tie.n_failing == 1
    assert tie.status == "gl_variances_outside_tolerance"
    assert "net_sales" in tie.account_mapping and "cogs" in tie.account_mapping
    # Trace evidence: line -> invoice -> period rows with both totals beside.
    assert tie.trace_sample is not None
    t0 = tie.trace_sample.to_dicts()[0]
    assert t0["period_engine_net_sales_cents"] == JAN_NET
    assert t0["period_gl_net_sales_cents"] == JAN_NET
