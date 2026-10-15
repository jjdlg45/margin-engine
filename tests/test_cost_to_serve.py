"""Phase 13 / 17.1 verification: activity costs, returns-inclusive economics,
coverage identity, planted unprofitable small-order customers."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.extended.cost_to_serve import run_cost_to_serve
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline
from tests.fixtures.builder import FixtureWorld

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def test_activity_costs_hand_calc(tmp_path: Path) -> None:
    """Order with 2 lines: per-order 18.00 + per-delivery 9.00 = 27.00 split
    13.50/13.50; plus 1.50 per line. Activity sits in its own labeled column;
    the BASE contribution is untouched; after-activity subtracts it."""
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    for _ in range(2):
        w.add_line(
            INV_NO="INV1",
            ORD_NO="ORD1",
            CUST_NO="C1",
            ITEM_NO="X1",
            QTY_SHP=5,
            UNIT_PRC="100.00",
            EXT_NET="500.00",
            LIST_PRC="100.00",
            UNIT_CST="60.0000",
        )
    res = run_pipeline(w.write(), MAPPING, Rules())
    cts = run_cost_to_serve(res.ledger, Rules())
    rows = cts.lines.filter(pl.col("transaction_type") == "sale").to_dicts()
    for r in rows:
        assert r["activity_cts_cents"] == 1_350 + 150
        assert r["activity_cts_status"] == "estimated"
        # Base contribution: pocket - cost - waterfall CTS (no activity).
        assert r["contribution_cents"] == 50_000 - 30_000
        assert r["contribution_after_activity_cents"] == 20_000 - 1_500
    order = cts.order_economics.to_dicts()[0]
    assert order["activity_cts_cents"] == 3_000
    assert order["contribution_cents"] == 40_000
    assert order["contribution_after_activity_cents"] == 40_000 - 3_000


def test_order_economics_include_linked_returns(tmp_path: Path) -> None:
    """Returns join their original order: sales and returns contribution shown
    separately plus the net."""
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "80.00")
    w.add_line(
        INV_NO="INV1",
        ORD_NO="ORD1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=10,
        UNIT_PRC="80.00",
        EXT_NET="800.00",
        LIST_PRC="80.00",
        UNIT_CST="40.0000",
    )
    w.add_line(
        INV_NO="RET1",
        ORD_NO="ORD1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-4,
        UNIT_PRC="80.00",
        EXT_NET="-320.00",
        LIST_PRC="80.00",
        UNIT_CST="40.0000",
        TXN_CD="R",
        ORIG_INV="INV1",
        INV_DT="04/10/2024",
        REASON_CD="DEFECT",
    )
    res = run_pipeline(w.write(), MAPPING, Rules())
    cts = run_cost_to_serve(res.ledger, Rules())
    order = cts.order_economics.to_dicts()[0]
    assert order["n_sale_lines"] == 1 and order["n_return_lines"] == 1
    assert order["sales_net_cents"] == 80_000
    assert order["returns_net_cents"] == -32_000
    assert order["net_cents"] == 48_000
    # Return contribution: pocket -320.00 - cost(-160.00) - CTS (handling
    # 1500+4x50=1700 + default freight est 1200) = -32000+16000-2900 = -18900.
    assert order["returns_contribution_cents"] == -18_900
    assert order["sales_contribution_cents"] == 80_000 - 40_000
    assert (
        order["contribution_cents"]
        == order["sales_contribution_cents"] + order["returns_contribution_cents"]
    )


def test_coverage_identity_property(tmp_path: Path) -> None:
    """Property over a full generated world: for EVERY order and customer row,
    pocket_covered - product_cost - cts_covered == contribution (exact), the
    after-activity measure equals contribution - activity, and excluded revenue
    is visible whenever costs are missing."""
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=8, n_customers=80, n_items=200)
    generate(cfg, tmp_path)
    rules = load_rules(REPO / "config" / "rules.yaml")
    res = run_pipeline(tmp_path / "erp", MAPPING, rules)
    cts = run_cost_to_serve(res.ledger, rules)
    for frame in (cts.order_economics, cts.customer_economics):
        bad = frame.filter(
            (
                pl.col("pocket_covered_cents")
                - pl.col("product_cost_cents")
                - pl.col("cts_covered_cents")
            )
            != pl.col("contribution_cents")
        )
        assert bad.height == 0
        bad2 = frame.filter(
            (pl.col("contribution_cents") - pl.col("activity_cts_cents"))
            != pl.col("contribution_after_activity_cents")
        )
        assert bad2.height == 0
        assert (frame["activity_cts_status"] == "estimated").all()
    # Missing-cost lines exist in this world, so some exclusion must show.
    assert cts.customer_economics.filter(pl.col("pct_revenue_excluded") > 0).height > 0
    assert (
        cts.customer_economics["revenue_with_cost_cents"]
        <= cts.customer_economics["net_cents"].abs() + cts.customer_economics["net_cents"].abs()
    ).all()


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PipelineResult]:
    out = tmp_path_factory.mktemp("cts")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=8, n_customers=80, n_items=200)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, load_rules(REPO / "config" / "rules.yaml"))
    return out, res


def test_planted_small_order_customers_identified(
    generated: tuple[Path, PipelineResult],
) -> None:
    out, res = generated
    truth = json.loads((out / "ground_truth" / "truth_summary.json").read_text())
    planted = set(truth["planted_small_order_customers"])
    assert planted
    cts = run_cost_to_serve(res.ledger, load_rules(REPO / "config" / "rules.yaml"))
    flagged = set(cts.service_flags["customer_id"].to_list())
    missed = planted - flagged
    assert not missed, f"planted small-order customers not flagged: {missed}"
    assert (cts.service_flags["opportunity_category"] == "service_policy_not_pricing").all()


def test_activity_conservation(generated: tuple[Path, PipelineResult]) -> None:
    _, res = generated
    rules = load_rules(REPO / "config" / "rules.yaml")
    cts = run_cost_to_serve(res.ledger, rules)
    sale = cts.lines.filter(pl.col("transaction_type") == "sale")
    c = rules.cost_to_serve
    n_orders = sale["order_id"].n_unique()
    n_lines = sale.height
    expected = n_orders * (c.per_order_cents + c.per_delivery_cents) + n_lines * c.per_line_cents
    assert int(sale["activity_cts_cents"].sum()) == expected
    # Order totals == line sums for the full-basis columns.
    assert int(cts.order_economics["net_cents"].sum()) == int(
        cts.lines.filter(pl.col("transaction_type").is_in(["sale", "return"]))[
            "net_amount_cents"
        ].sum()
    )
