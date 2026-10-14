"""Property tests: whatever the quantities, prices, signs and deductions,
every dollar reconciles to source totals (Phase 2).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from margin_engine.config import Rules
from margin_engine.ledger.pipeline import run_pipeline
from tests.fixtures.builder import FixtureWorld

MAPPING = Path(__file__).resolve().parents[2] / "config" / "mappings" / "synth_erp.yaml"

money = st.integers(min_value=1, max_value=50_000_000)  # cents
small_money = st.integers(min_value=0, max_value=100_000)

line_strategy = st.fixed_dictionaries(
    {
        "qty": st.integers(min_value=1, max_value=500),
        "price_cents": st.integers(min_value=1, max_value=1_000_000),
        "cost_cents": st.one_of(st.none(), st.integers(min_value=0, max_value=800_000)),
        "rebate_cents": small_money,
        "concession_cents": small_money,
        "txn": st.sampled_from(["S", "S", "S", "R"]),
    }
)


@st.composite
def worlds(draw: st.DrawFn) -> list[dict[str, int | str | None]]:
    return draw(st.lists(line_strategy, min_size=1, max_size=25))


def _fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}{c // 100}.{c % 100:02d}"


@given(rows=worlds(), settlement_cents=st.integers(min_value=0, max_value=200_000))
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_always_reconciles(
    tmp_path: Path, rows: list[dict[str, int | str | None]], settlement_cents: int
) -> None:
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    expected_net = 0
    for i, r in enumerate(rows):
        qty = int(r["qty"]) * (1 if r["txn"] == "S" else -1)
        net = qty * int(r["price_cents"])
        expected_net += net
        w.add_line(
            INV_NO=f"INV{i}",
            ORD_NO=f"ORD{i % 3}",
            CUST_NO="C1",
            ITEM_NO="X1",
            QTY_SHP=qty,
            UNIT_PRC=_fmt(int(r["price_cents"])),
            EXT_NET=_fmt(net),
            LIST_PRC="100.00",
            UNIT_CST="" if r["cost_cents"] is None else _fmt(int(r["cost_cents"])),
            TXN_CD=str(r["txn"]),
            ORIG_INV="INV0" if r["txn"] == "R" else "",
            REB_AMT=_fmt(int(r["rebate_cents"]) if r["txn"] == "S" else 0),
            FRT_CONC=_fmt(int(r["concession_cents"]) if r["txn"] == "S" else 0),
        )
    has_positive_sale = any(r["txn"] == "S" for r in rows)
    if settlement_cents and has_positive_sale:
        w.rebate_settlements.append(
            {"CUST_NO": "C1", "PERIOD": "2024-Q1", "PGM_ID": "RP1", "AMT": _fmt(settlement_cents)}
        )
    data = w.write()
    res = run_pipeline(data, MAPPING, Rules())

    # Reconciliation ran without raising; assert the headline conservation too.
    assert res.reconciliation.all_pass
    ledger_net = int(res.ledger["net_amount_cents"].sum()) + int(
        res.removed_duplicates["net_amount_cents"].sum() if res.removed_duplicates.height else 0
    )
    assert ledger_net == expected_net

    # Settlement allocation conserves the settlement total exactly.
    if settlement_cents and has_positive_sale:
        allocated = int(res.ledger["rebate_settlement_alloc_cents"].fill_null(0).sum())
        residual = res.reconciliation.residuals.get("settlement_rebates_unallocated_cents", 0)
        assert allocated + residual == settlement_cents

    # Pocket identity on sale lines.
    sale = res.ledger.filter(pl.col("transaction_type") == "sale")
    identity = sale.select(
        (
            pl.col("pocket_revenue_cents")
            - (
                pl.col("invoice_revenue_cents")
                - pl.col("rebate_actual_cents")
                - pl.col("rebate_settlement_alloc_cents")
                - pl.col("freight_concession_cents")
                + pl.col("price_credit_alloc_cents")
            )
        )
        .abs()
        .sum()
    ).item()
    assert identity == 0
