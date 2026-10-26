"""Phase 61 (Run 6): economic event taxonomy — edge-case fixtures.

Every case is hand-calculable from the numbers in the test body. The ledger
always reconciles to source totals; the event taxonomy classifies adjustments
on top of it and never moves a dollar.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import yaml

from margin_engine.config import Rules
from margin_engine.guidance.bands import build_guidance
from margin_engine.ledger.events import (
    EVENT_TYPES,
    EventsResult,
    build_events,
    vendor_rebate_rollforward,
)
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline
from tests.fixtures.builder import FixtureWorld

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


def run_world(
    world: FixtureWorld, rules: Rules | None = None, mapping: Path = MAPPING
) -> tuple[PipelineResult, EventsResult]:
    rules = rules or Rules()
    res = run_pipeline(world.write(), mapping, rules)
    assert res.reconciliation.all_pass
    events = build_events(
        res.ledger,
        rules=rules,
        rebate_settlements=res.reference["rebate_settlements"],
        order_charges=res.reference["order_charges"],
        vendor_rebate_programs=res.reference["vendor_rebate_programs"],
        tiered_summary=res.tiered_summary,
    )
    return res, events


def _base_sale(w: FixtureWorld, *, net: str = "950.00", cost: str = "60.0000") -> None:
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=10,
        UNIT_PRC="95.00",
        EXT_NET=net,
        LIST_PRC="100.00",
        UNIT_CST=cost,
    )


# ---------------------------------------------------------------------------
# 61 edge case: credit memo posted to the original invoice AND present as a
# separate credit document — counted once in event totals, every source dollar
# kept in the ledger.
# ---------------------------------------------------------------------------


def test_duplicate_credit_representations_counted_once(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    _base_sale(w)
    # Embedded concession: a negative line posted ON the original invoice.
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=0,
        UNIT_PRC="0",
        EXT_NET="-50.00",
    )
    # The same credit again as a standalone credit document.
    w.add_line(
        INV_NO="CM1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=0,
        UNIT_PRC="0",
        EXT_NET="-50.00",
        TXN_CD="C",
        REASON_CD="price_correction",
        ORIG_INV="INV1",
    )
    res, ev = run_world(w)
    # Both rows keep their dollars (source reconciliation), the credit row is
    # flagged as a duplicate representation.
    flagged = res.ledger.filter(pl.col("flag_duplicate_adjustment_suspect"))
    assert flagged.height == 1
    assert flagged["source_line_key"][0] == "L3"
    # The standalone credit's event exists but is a duplicate suspect, so the
    # concession totals count the 50.00 once (via the embedded posting's
    # pocket effect) and the excluded amount is reported.
    credit_events = ev.events.filter(pl.col("event_subtype") == "price_credit_memo")
    assert credit_events.height == 1
    assert bool(credit_events["duplicate_suspect"][0])
    assert ev.duplicate_excluded_cents == 5_000
    totals = dict(ev.totals.select("event_type", "amount_cents").iter_rows())
    assert totals.get("customer_price_concession", 0) == 0  # only the dup credit existed


def test_distinct_credits_are_not_duplicate_suspects(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    _base_sale(w)
    w.add_line(
        INV_NO="CM1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=0,
        UNIT_PRC="0",
        EXT_NET="-50.00",
        TXN_CD="C",
        REASON_CD="price_correction",
        ORIG_INV="INV1",
    )
    res, ev = run_world(w)
    assert res.ledger.filter(pl.col("flag_duplicate_adjustment_suspect")).height == 0
    assert ev.duplicate_excluded_cents == 0
    totals = dict(ev.totals.select("event_type", "amount_cents").iter_rows())
    assert totals["customer_price_concession"] == 5_000


# ---------------------------------------------------------------------------
# 61 edge case: voided-and-replaced invoice.
# ---------------------------------------------------------------------------


def test_voided_and_replaced_invoice(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    _base_sale(w)  # INV1, 950.00
    w.add_line(  # full credit voiding INV1
        INV_NO="CM1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=0,
        UNIT_PRC="0",
        EXT_NET="-950.00",
        TXN_CD="C",
        REASON_CD="price_correction",
        ORIG_INV="INV1",
    )
    w.add_line(  # replacement invoice
        INV_NO="INV2",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=10,
        UNIT_PRC="96.00",
        EXT_NET="960.00",
        LIST_PRC="100.00",
        UNIT_CST="60.0000",
    )
    res, ev = run_world(w)
    rows = {r["source_line_key"]: r for r in res.ledger.to_dicts()}
    # The void credit attributes fully to INV1: its pocket nets to zero and
    # only the replacement invoice carries revenue.
    assert rows["L1"]["pocket_revenue_cents"] == 0
    assert rows["L3"]["pocket_revenue_cents"] == 96_000
    assert bool(rows["L2"]["credit_attributed"])
    sale_pocket = int(
        res.ledger.filter(pl.col("transaction_type") == "sale")["pocket_revenue_cents"].sum()
    )
    assert sale_pocket == 96_000
    # The event carries the reversal link to the voided invoice.
    credit_event = ev.events.filter(pl.col("event_subtype") == "price_credit_memo").to_dicts()[0]
    assert credit_event["reversal_link"] == "INV1"
    assert credit_event["amount_cents"] == 95_000
    assert not credit_event["duplicate_suspect"]


# ---------------------------------------------------------------------------
# 61 edge case: a customer claim caused by supplier damage — the customer
# claim is a cost, the supplier recovery reduces cost, and neither touches
# customer revenue.
# ---------------------------------------------------------------------------


def test_customer_claim_with_supplier_recovery(tmp_path: Path) -> None:
    rules = Rules.model_validate(
        {"transactions": {"supplier_claim_reasons": ["supplier_damage_recovery"]}}
    )
    w = FixtureWorld(tmp_path)
    _base_sale(w, net="1000.00")
    w.add_line(  # customer damage claim: $80 cost, not a price change
        INV_NO="CM1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=0,
        UNIT_PRC="0",
        EXT_NET="-80.00",
        TXN_CD="C",
        REASON_CD="damage",
        ORIG_INV="INV1",
    )
    w.add_line(  # recovery FROM the supplier who caused it: +$50
        INV_NO="CM2",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=0,
        UNIT_PRC="0",
        EXT_NET="50.00",
        TXN_CD="C",
        REASON_CD="supplier_damage_recovery",
        ORIG_INV="INV1",
    )
    res, ev = run_world(w, rules)
    rows = {r["source_line_key"]: r for r in res.ledger.to_dicts()}
    # Customer revenue untouched by either credit.
    assert rows["L1"]["pocket_revenue_cents"] == 100_000
    # Net cost effect on the original line: 80.00 claim - 50.00 recovery.
    assert rows["L1"]["claims_cost_alloc_cents"] == 3_000
    assert rows["L1"]["cost_to_serve_cents"] == 3_000
    # Events: the claim is a customer cost; the recovery names the supplier.
    claim = ev.events.filter(pl.col("event_type") == "non_price_customer_claim").to_dicts()[0]
    assert claim["amount_cents"] == 8_000
    assert claim["direction"] == "cost_increase"
    assert claim["counterparty"] == "customer"
    rec = ev.events.filter(pl.col("event_type") == "supplier_quality_claim").to_dicts()[0]
    assert rec["amount_cents"] == 5_000
    assert rec["direction"] == "cost_reduction"
    assert rec["counterparty"] == "supplier"
    assert rec["reversal_link"] == "INV1"


# ---------------------------------------------------------------------------
# 61 edge case: supplier rebate with inventory still on hand — the vendor
# rebate roll-forward separates earned-on-receipts from recognized-in-COGS.
# ---------------------------------------------------------------------------


def test_vendor_rollforward_inventory_on_hand(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    w.add_item("X1", supplier="SUPA")
    w.add_list_price("X1", "01/01/2024", "100.00")
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=60,
        UNIT_PRC="95.00",
        EXT_NET="5700.00",
        LIST_PRC="100.00",
        UNIT_CST="10.0000",
    )
    w.vendor_rebate_programs = [{"SUPPLIER": "SUPA", "RATE_PCT": "5.0"}]
    w.receipts = [
        {"ITEM_NO": "X1", "RCPT_DT": "2024-01-05", "RCPT_QTY": "100", "UNIT_CST": "10.00"}
    ]
    res, ev = run_world(w)
    row = res.ledger.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    # Netting at sale: 5% of 60 x $10.00 = $30.00 off product cost.
    assert row["vendor_rebate_cents"] == 3_000
    assert row["product_cost_cents"] == 60_000 - 3_000
    roll = vendor_rebate_rollforward(
        res.ledger,
        res.reference["inventory_receipts"],
        res.reference["items"],
        res.reference["vendor_rebate_programs"],
    )
    assert roll.table is not None
    # Earned on 100 received units in January: $50.00; recognized on 60 sold
    # in March: $30.00; the running balance leaves $20.00 on the 40 units
    # still on hand.
    assert int(roll.table["earned_on_receipts_cents"].sum()) == 5_000
    assert int(roll.table["recognized_in_cogs_cents"].sum()) == 3_000
    last = roll.table.to_dicts()[-1]
    assert last["balance_on_inventory_cents"] == 2_000
    assert last["status"] == "estimated"
    vend = ev.events.filter(pl.col("event_type") == "supplier_rebate_allowance").to_dicts()[0]
    assert vend["amount_cents"] == 3_000
    assert vend["counterparty"] == "supplier"


def test_vendor_rollforward_not_testable_without_receipts(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    _base_sale(w)
    w.vendor_rebate_programs = [{"SUPPLIER": "SUPA", "RATE_PCT": "5.0"}]
    res, _ = run_world(w)
    roll = vendor_rebate_rollforward(
        res.ledger, None, res.reference["items"], res.reference["vendor_rebate_programs"]
    )
    assert roll.table is None
    assert any("not testable" in n for n in roll.notes)


# ---------------------------------------------------------------------------
# 61 edge case: vendor payment for a distinct marketing service (ASC 705-20
# exception) — never nets product cost, reported as its own event.
# ---------------------------------------------------------------------------


def _mapping_with_consideration(tmp_path: Path) -> Path:
    raw = yaml.safe_load(MAPPING.read_text(encoding="utf-8"))
    raw["reference_tables"]["vendor_rebate_programs"]["columns"]["consideration_type"] = "CONSID"
    out = tmp_path / "mapping_consid.yaml"
    out.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return out


def test_vendor_distinct_service_never_nets_cost(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    w.add_item("X1", supplier="SUPA")
    w.add_list_price("X1", "01/01/2024", "100.00")
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=60,
        UNIT_PRC="95.00",
        EXT_NET="5700.00",
        LIST_PRC="100.00",
        UNIT_CST="10.0000",
    )
    w.vendor_rebate_programs = [
        {"SUPPLIER": "SUPA", "RATE_PCT": "5.0", "CONSID": "distinct_service"}
    ]
    mapping = _mapping_with_consideration(tmp_path)
    res, ev = run_world(w, mapping=mapping)
    row = res.ledger.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    # Product cost carries NO netting: the payment is for a distinct service.
    assert row["vendor_rebate_cents"] == 0
    assert row["product_cost_cents"] == 60_000
    exc = ev.events.filter(
        pl.col("event_type") == "vendor_distinct_service_reimbursement"
    ).to_dicts()
    assert len(exc) == 1
    assert exc[0]["amount_cents"] == 3_000  # 5% of the sold base cost, estimated
    assert exc[0]["amount_status"] == "estimated"
    assert exc[0]["direction"] == "other_income"


# ---------------------------------------------------------------------------
# 61 edge case: negative net rebate basis (returns exceed purchases).
# ---------------------------------------------------------------------------


def test_negative_rebate_basis_fails_closed(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "60.00")
    # Return-only year for a tiered customer: eligible basis is negative.
    w.add_line(
        INV_NO="RET1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-15,
        UNIT_PRC="60.00",
        EXT_NET="-900.00",
        TXN_CD="R",
        UNIT_CST="30.0000",
    )
    w.rebate_programs = [{"PGM_ID": "TIER01", "CUST_NO": "C1", "KIND": "tiered", "RATE_PCT": "0.0"}]
    w.rebate_program_tiers = [
        {"PGM_ID": "TIER01", "MIN_AMT": "0.00", "RATE_PCT": "1.0"},
        {"PGM_ID": "TIER01", "MIN_AMT": "1000.00", "RATE_PCT": "2.0"},
    ]
    res, ev = run_world(w)
    row = res.ledger.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    # No rebate is invented on a negative basis: rate 0, accrual 0.
    assert row["tiered_rate_bp"] == 0
    assert row["tiered_accrued_cents"] == 0
    assert res.tiered_summary is not None
    s = res.tiered_summary.to_dicts()[0]
    assert s["eligible_basis_cents"] == -90_000
    assert s["negative_basis"] is True
    acc = ev.events.filter(pl.col("event_subtype") == "tiered_rebate_accrual").to_dicts()[0]
    assert acc["amount_cents"] == 0
    assert acc["amount_status"] == "fail_closed_negative_basis"


# ---------------------------------------------------------------------------
# 61 edge case: multiple simultaneous programs with overlapping eligibility —
# per-program accrual, summed per line, no row duplication, no double count.
# ---------------------------------------------------------------------------


def _two_program_world(tmp_path: Path, *, settle_one: bool) -> FixtureWorld:
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "60.00")
    for i, dt in enumerate(["02/10/2024", "07/10/2024"]):
        w.add_line(
            INV_NO=f"INV{i}",
            CUST_NO="C1",
            ITEM_NO="X1",
            QTY_SHP=10,
            UNIT_PRC="60.00",
            EXT_NET="600.00",
            LIST_PRC="60.00",
            UNIT_CST="30.0000",
            INV_DT=dt,
        )
    w.rebate_programs = [
        {"PGM_ID": "TIER01", "CUST_NO": "C1", "KIND": "tiered", "RATE_PCT": "0.0"},
        {"PGM_ID": "TIER02", "CUST_NO": "C1", "KIND": "tiered", "RATE_PCT": "0.0"},
    ]
    w.rebate_program_tiers = [
        {"PGM_ID": "TIER01", "MIN_AMT": "0.00", "RATE_PCT": "1.0"},
        {"PGM_ID": "TIER01", "MIN_AMT": "1000.00", "RATE_PCT": "2.0"},
        {"PGM_ID": "TIER02", "MIN_AMT": "0.00", "RATE_PCT": "0.5"},
    ]
    if settle_one:
        w.tiered_settlements = [
            {"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "24.00"}
        ]
    return w


def test_multiple_programs_accrue_without_double_counting(tmp_path: Path) -> None:
    w = _two_program_world(tmp_path, settle_one=False)
    res, ev = run_world(w)
    assert res.ledger.height == 2  # no row duplication from the two programs
    rows = {r["source_line_key"]: r for r in res.ledger.to_dicts()}
    # Line 1: TIER01 1% x 600 = 6.00 + TIER02 0.5% x 600 = 3.00 -> 9.00.
    assert rows["L1"]["tiered_rate_bp"] == 150
    assert rows["L1"]["tiered_accrued_cents"] == 900
    # Line 2: TIER01 2% (cum 1200) + TIER02 0.5% -> 12.00 + 3.00 = 15.00.
    assert rows["L2"]["tiered_rate_bp"] == 250
    assert rows["L2"]["tiered_accrued_cents"] == 1_500
    assert res.tiered_summary is not None
    assert res.tiered_summary.height == 2  # one row per program
    accs = ev.events.filter(pl.col("event_subtype") == "tiered_rebate_accrual")
    assert accs.height == 2
    assert set(accs["program_id"].to_list()) == {"TIER01", "TIER02"}


def test_partially_settled_programs_mix_actual_and_accrual(tmp_path: Path) -> None:
    w = _two_program_world(tmp_path, settle_one=True)
    res, ev = run_world(w)
    rows = {r["source_line_key"]: r for r in res.ledger.to_dicts()}
    # TIER01 settles $24.00 (12.00 per line); TIER02 stays accrued (3.00/line).
    for k in ("L1", "L2"):
        assert rows[k]["tiered_actual_alloc_cents"] == 1_200
        assert rows[k]["tiered_rebate_used_cents"] == 1_200 + 300
        assert rows[k]["tiered_rebate_status"] == "partially_settled"
    # True-up on the settled program only: 2400 actual - 1800 accrued.
    assert res.reconciliation.residuals["tiered_true_up_cents"] == 600
    tset = ev.events.filter(pl.col("event_subtype") == "tiered_rebate_settlement").to_dicts()
    assert len(tset) == 1
    assert tset[0]["program_id"] == "TIER01"
    assert tset[0]["reversal_link"] == "tacc|C1|2024|TIER01"


def test_settlement_for_unheld_program_is_residual(tmp_path: Path) -> None:
    w = _two_program_world(tmp_path, settle_one=False)
    w.tiered_settlements = [
        {"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER99", "AMT": "9.00"}
    ]
    res, _ = run_world(w)
    assert res.reconciliation.residuals["tiered_settlements_unallocated_cents"] == 900


def test_guidance_deduction_stacks_programs_without_duplication() -> None:
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 1}})
    n = 10
    df = pl.DataFrame(
        {
            "customer_id": ["C1"] * n,
            "item_id": [f"I{i}" for i in range(n)],
            "segment_leaf": [1] * n,
            "price_index": [0.90] * n,
            "invoice_date": pl.Series([date(2024, 1, 15)] * n, dtype=pl.Date),
            "quantity_norm": [1.0] * n,
            "product_cost_cents": [5_000] * n,
            "eff_list_cents": [10_000] * n,
            "contract_id": [None] * n,
            "flag_data_quality_anomaly": [False] * n,
        }
    )
    programs = pl.DataFrame(
        {
            "customer_id": ["C1", "C1"],
            "rebate_program_id": ["P1", "P2"],
            "program_kind": ["pct", "pct"],
            "rate_pct": [2.0, 1.5],
        }
    )
    result = build_guidance(df, rules, programs)
    # One guidance row per pair (the two programs never duplicate pairs)...
    assert result.guidance.height == n
    # ...and d carries the SUM of the simultaneous program rates: 350 bp.
    assert (result.guidance["deduction_bp"] == 350).all()


# ---------------------------------------------------------------------------
# Billed order charges: classified only by declared configuration.
# ---------------------------------------------------------------------------


def test_order_charges_classified_by_config_never_guessed(tmp_path: Path) -> None:
    rules = Rules.model_validate(
        {"transactions": {"charge_event_types": {"FRT": "freight_pass_through"}}}
    )
    w = FixtureWorld(tmp_path)
    _base_sale(w)
    w.order_charges = [
        {"ORD_NO": "ORD1", "CHG_CD": "FRT", "AMT": "20.00"},
        {"ORD_NO": "ORD1", "CHG_CD": "MISC", "AMT": "5.00"},
    ]
    _, ev = run_world(w, rules)
    frt = ev.events.filter(pl.col("event_type") == "freight_pass_through")
    assert frt.height == 1
    assert frt["amount_cents"][0] == 2_000
    other = ev.events.filter(
        (pl.col("event_type") == "other_adjustment")
        & (pl.col("event_subtype") == "billed_order_charge:MISC")
    )
    assert other.height == 1
    assert any("never guesses what a charge code means" in n for n in ev.notes)


def test_charge_map_to_unknown_type_fails_actionably(tmp_path: Path) -> None:
    rules = Rules.model_validate({"transactions": {"charge_event_types": {"FRT": "not_a_type"}}})
    w = FixtureWorld(tmp_path)
    _base_sale(w)
    w.order_charges = [{"ORD_NO": "ORD1", "CHG_CD": "FRT", "AMT": "20.00"}]
    res = run_pipeline(w.write(), MAPPING, rules)
    try:
        build_events(
            res.ledger,
            rules=rules,
            order_charges=res.reference["order_charges"],
        )
        raise AssertionError("expected a ValueError naming the unknown event type")
    except ValueError as exc:
        assert "not_a_type" in str(exc)
        assert "valid types" in str(exc)


# ---------------------------------------------------------------------------
# Taxonomy integrity.
# ---------------------------------------------------------------------------


def test_every_event_fully_described(tmp_path: Path) -> None:
    """Every event carries type, counterparty, direction, source reference,
    period and status; types stay within the declared taxonomy."""
    w = FixtureWorld(tmp_path)
    _base_sale(w)
    w.add_line(
        INV_NO="RET1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-2,
        UNIT_PRC="95.00",
        EXT_NET="-190.00",
        TXN_CD="R",
        REASON_CD="CUST_ERR",
        ORIG_INV="INV1",
        UNIT_CST="60.0000",
        RESTOCK_FEE="28.50",
    )
    _, ev = run_world(w)
    assert ev.events.height >= 2  # at least the return and its recovery
    assert set(ev.events["event_type"].to_list()) <= set(EVENT_TYPES)
    for col in ("counterparty", "direction", "source_ref", "amount_status", "event_id"):
        assert ev.events[col].null_count() == 0, col
    ret = ev.events.filter(pl.col("event_type") == "customer_refund_return").to_dicts()[0]
    assert ret["amount_cents"] == 19_000
    assert ret["reversal_link"] == "INV1"
    rec = ev.events.filter(pl.col("event_type") == "customer_service_payment").to_dicts()[0]
    assert rec["amount_cents"] == 2_850  # the restocking fee billed back
    assert rec["direction"] == "revenue_increase"
