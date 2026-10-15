"""Phase 13 verification: activity costs reconcile, order/customer economics,
planted unprofitable small-order customers identified, flags separate from
pricing."""

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
    13.50/13.50; plus 1.50 per line. Totals reconcile to the configured rates."""
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    for _i in range(2):
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
    order = cts.order_economics.to_dicts()[0]
    assert order["n_lines"] == 2
    # Order activity total = 2700 + 2x150 = 3000 (exact conservation).
    total_activity = sum(r["activity_cts_cents"] for r in rows)
    assert total_activity == 3_000
    assert (
        order["contribution_cents"]
        == order["pocket_cents"] - order["product_cost_cents"] - order["cost_to_serve_cents"]
    )


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
    # Flags are service-policy, never pricing.
    assert (cts.service_flags["opportunity_category"] == "service_policy_not_pricing").all()


def test_order_and_customer_economics_reconcile(
    generated: tuple[Path, PipelineResult],
) -> None:
    _, res = generated
    rules = load_rules(REPO / "config" / "rules.yaml")
    cts = run_cost_to_serve(res.ledger, rules)
    sale = cts.lines.filter(pl.col("transaction_type") == "sale")
    # Order totals == line sums (exact).
    assert int(cts.order_economics["net_cents"].sum()) == int(sale["net_amount_cents"].sum())
    assert int(cts.order_economics["cost_to_serve_cents"].sum()) == int(
        sale["cost_to_serve_cents"].sum()
    )
    # Customer totals == order sums.
    assert int(cts.customer_economics["net_cents"].sum()) == int(
        cts.order_economics["net_cents"].sum()
    )
    # Activity conservation: per-order rates x orders + per-line x lines.
    c = rules.cost_to_serve
    n_orders = cts.order_economics.height
    n_lines = sale.height
    expected = n_orders * (c.per_order_cents + c.per_delivery_cents) + n_lines * c.per_line_cents
    assert int(sale["activity_cts_cents"].sum()) == expected
