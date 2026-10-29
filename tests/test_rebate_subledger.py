"""Phase 62 (Run 6): rebate lifecycle subledger — hand-calculable fixtures.

Invariant under test everywhere: opening + estimates + revisions - settlements
- reversals = closing, per customer x program x period; each settlement row is
applied exactly once and replaces the estimates it settles.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from margin_engine.config import Rules
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline
from margin_engine.ledger.rebate_subledger import (
    SubledgerError,
    SubledgerResult,
    build_rebate_subledger,
    compute_balances,
    validate_subledger,
)
from tests.fixtures.builder import FixtureWorld

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


def run_subledger(world: FixtureWorld) -> tuple[PipelineResult, SubledgerResult]:
    res = run_pipeline(world.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    sub = build_rebate_subledger(
        res.ledger,
        res.reference["rebate_programs"],
        res.reference["rebate_settlements"],
        res.tiered_monthly,
        res.tiered_summary,
    )
    return res, sub


def _bal(sub: SubledgerResult, period: str) -> dict[str, object]:
    return sub.balances.filter(pl.col("period") == period).to_dicts()[0]


# ---------------------------------------------------------------------------
# Fixture: monthly estimate settled quarterly, then retroactively changed.
# ---------------------------------------------------------------------------


def _settlement_world(tmp_path: Path, settlements: list[dict[str, str]]) -> FixtureWorld:
    """Flat 2% settlement program; sales Jan 600, Feb 400, Mar 1000."""
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    for i, (dt, net) in enumerate(
        [("01/10/2024", "600.00"), ("02/10/2024", "400.00"), ("03/10/2024", "1000.00")]
    ):
        w.add_line(
            INV_NO=f"INV{i}",
            CUST_NO="C1",
            ITEM_NO="X1",
            QTY_SHP=10,
            UNIT_PRC="60.00",
            EXT_NET=net,
            LIST_PRC="100.00",
            UNIT_CST="30.0000",
            INV_DT=dt,
        )
    w.rebate_programs = [
        {"PGM_ID": "RP001", "CUST_NO": "C1", "KIND": "settlement", "RATE_PCT": "2.0"}
    ]
    w.rebate_settlements = settlements
    return w


def test_monthly_estimates_settled_quarterly(tmp_path: Path) -> None:
    # Estimates: 2% of 600 / 400 / 1000 = 12.00, 8.00, 20.00 (40.00 total).
    # Settlement Q1 exactly 40.00: applied once, true-up zero, closing zero.
    w = _settlement_world(
        tmp_path, [{"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RP001", "AMT": "40.00"}]
    )
    _, sub = run_subledger(w)
    est = sub.entries.filter(pl.col("entry_type") == "estimate")
    assert est["amount_cents"].to_list() == [1_200, 800, 2_000]
    setts = sub.entries.filter(pl.col("entry_type") == "settlement")
    assert setts.height == 1
    assert setts["amount_cents"][0] == 4_000
    assert setts["period"][0] == "2024-03"  # quarter-end month
    assert sub.entries.filter(pl.col("entry_type") == "revision").height == 0  # no true-up
    assert _bal(sub, "2024-03")["closing_cents"] == 0


def test_settlement_retroactively_changed(tmp_path: Path) -> None:
    # Original settlement 40.00 plus a later amendment +10.00 for the same
    # quarter: two settlement rows, each applied once; the true-up revision
    # books the +10.00 the estimates never carried; closing returns to zero.
    w = _settlement_world(
        tmp_path,
        [
            {"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RP001", "AMT": "40.00"},
            {"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RP001", "AMT": "10.00"},
        ],
    )
    _, sub = run_subledger(w)
    setts = sub.entries.filter(pl.col("entry_type") == "settlement")
    assert setts.height == 2
    assert sorted(setts["amount_cents"].to_list()) == [1_000, 4_000]
    assert setts["source_ref"].n_unique() == 2  # each row applied exactly once
    tu = sub.entries.filter(pl.col("entry_type") == "revision").to_dicts()[0]
    assert tu["amount_cents"] == 1_000
    assert "true-up" in tu["note"]
    assert _bal(sub, "2024-03")["closing_cents"] == 0


# ---------------------------------------------------------------------------
# Fixture: tier crossing changes previously accrued volume (retrospective
# top-up revision).
# ---------------------------------------------------------------------------


def _tiered_world(tmp_path: Path, *, settle_amt: str | None) -> FixtureWorld:
    """1%/2% tiers at $1000: 600.00 in Feb, 600.00 in Jul (crosses the tier)."""
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
    w.rebate_programs = [{"PGM_ID": "TIER01", "CUST_NO": "C1", "KIND": "tiered", "RATE_PCT": "0.0"}]
    w.rebate_program_tiers = [
        {"PGM_ID": "TIER01", "MIN_AMT": "0.00", "RATE_PCT": "1.0"},
        {"PGM_ID": "TIER01", "MIN_AMT": "1000.00", "RATE_PCT": "2.0"},
    ]
    if settle_amt is not None:
        w.tiered_settlements = [
            {"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": settle_amt}
        ]
    return w


def test_tier_crossing_books_retrospective_revision(tmp_path: Path) -> None:
    w = _tiered_world(tmp_path, settle_amt=None)
    _, sub = run_subledger(w)
    # Feb: estimate 1% x 600 = 6.00; no revision (target 1% x 600 = accrual).
    assert _bal(sub, "2024-02")["estimates_cents"] == 600
    assert _bal(sub, "2024-02")["revisions_cents"] == 0
    # Jul: estimate 2% x 600 = 12.00 PLUS the retrospective top-up: crossing
    # the tier re-rates the whole 1200.00 at 2% = 24.00, while accruals to
    # date are 18.00 -> revision +6.00.
    jul = _bal(sub, "2024-07")
    assert jul["estimates_cents"] == 1_200
    assert jul["revisions_cents"] == 600
    assert jul["closing_cents"] == 2_400  # the true retrospective liability


def test_tiered_settlement_replaces_estimates(tmp_path: Path) -> None:
    w = _tiered_world(tmp_path, settle_amt="24.00")
    _, sub = run_subledger(w)
    jul = _bal(sub, "2024-07")
    assert jul["settlements_cents"] == 2_400
    # Estimates (18.00) + retrospective revision (6.00) == settlement: the
    # year closes at zero with no further true-up needed.
    assert jul["closing_cents"] == 0
    setts = sub.entries.filter(pl.col("entry_type") == "settlement")
    assert setts.height == 1
    assert setts["status"][0] == "actual"


# ---------------------------------------------------------------------------
# Fixture: clawback — a return after the rebate period and after settlement.
# ---------------------------------------------------------------------------


def test_return_after_settled_year_books_clawback(tmp_path: Path) -> None:
    w = _tiered_world(tmp_path, settle_amt="24.00")
    # A 2025 return referencing the settled 2024 invoice: 200.00 back at the
    # settled effective rate (24.00 / 1200.00 = 2%) -> 4.00 clawback.
    w.add_line(
        INV_NO="RET1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-3,
        UNIT_PRC="60.00",
        EXT_NET="-200.00",
        TXN_CD="R",
        ORIG_INV="INV0",
        INV_DT="01/15/2025",
        UNIT_CST="30.0000",
    )
    _, sub = run_subledger(w)
    claw = sub.entries.filter(pl.col("entry_type") == "reversal").to_dicts()
    assert len(claw) == 1
    assert claw[0]["amount_cents"] == 400
    assert claw[0]["period"] == "2025-01"
    assert claw[0]["status"] == "estimated"
    assert claw[0]["link"] == "C1|2024"
    # The 2024 year stays closed at zero; 2025 carries the negative balance
    # (a receivable from the customer) in plain sight.
    assert _bal(sub, "2025-01")["closing_cents"] < 0
    assert any("clawback" in n for n in sub.notes)


def test_return_after_period_before_settlement_lowers_accrual(tmp_path: Path) -> None:
    w = _tiered_world(tmp_path, settle_amt=None)
    w.add_line(
        INV_NO="RET1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-3,
        UNIT_PRC="60.00",
        EXT_NET="-200.00",
        TXN_CD="R",
        ORIG_INV="INV0",
        INV_DT="01/15/2025",
        UNIT_CST="30.0000",
    )
    _, sub = run_subledger(w)
    # Unsettled prior year: the return lowers the open accrual estimate via a
    # negative revision at the year-end tier rate (2% x 200 = 4.00).
    revs = sub.entries.filter(
        (pl.col("entry_type") == "revision") & (pl.col("period") == "2025-01")
    ).to_dicts()
    assert len(revs) == 1
    assert revs[0]["amount_cents"] == -400
    assert sub.entries.filter(pl.col("entry_type") == "reversal").height == 0


# ---------------------------------------------------------------------------
# Invariant enforcement.
# ---------------------------------------------------------------------------


def test_invariant_holds_on_every_period(tmp_path: Path) -> None:
    w = _settlement_world(
        tmp_path, [{"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RP001", "AMT": "45.00"}]
    )
    _, sub = run_subledger(w)
    b = sub.balances
    identity = b.filter(
        pl.col("closing_cents")
        != pl.col("opening_cents")
        + pl.col("estimates_cents")
        + pl.col("revisions_cents")
        - pl.col("settlements_cents")
        - pl.col("reversals_cents")
    )
    assert identity.height == 0
    # And openings chain to prior closings (validated internally too).
    validate_subledger(sub.entries, sub.balances)


def test_duplicate_settlement_application_raises() -> None:
    entries = pl.DataFrame(
        {
            "customer_id": ["C1", "C1"],
            "rebate_program_id": ["P1", "P1"],
            "period": ["2024-03", "2024-03"],
            "entry_type": ["settlement", "settlement"],
            "amount_cents": [1_000, 1_000],
            "status": ["actual", "actual"],
            "source_ref": ["settle|C1|2024-Q1#0", "settle|C1|2024-Q1#0"],
            "link": ["2024-Q1", "2024-Q1"],
            "note": ["", ""],
        }
    )
    with pytest.raises(SubledgerError, match="more than once"):
        validate_subledger(entries, compute_balances(entries))


def test_line_accrual_programs_stated_outside_subledger(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=10,
        UNIT_PRC="95.00",
        EXT_NET="950.00",
        LIST_PRC="100.00",
        UNIT_CST="60.0000",
        REB_AMT="19.00",
        REB_PGM="RP001",
    )
    w.rebate_programs = [
        {"PGM_ID": "RP001", "CUST_NO": "C1", "KIND": "line_accrual", "RATE_PCT": "2.0"}
    ]
    _, sub = run_subledger(w)
    assert sub.entries.height == 0
    assert any("outside the subledger" in n for n in sub.notes)
