"""Phase 14 verification: timing fixtures exact per costing method; value
conservation; honest timing statements."""

from __future__ import annotations

from datetime import date

import polars as pl

from margin_engine.extended.inventory import cost_sales, scenario_timing_statement


def _world() -> tuple[pl.DataFrame, pl.DataFrame]:
    receipts = pl.DataFrame(
        {
            "item_id": ["I1", "I1"],
            "receipt_date": pl.Series([date(2024, 1, 1), date(2024, 2, 1)], dtype=pl.Date),
            "quantity": [10, 10],
            "unit_cost_cents": [1_000, 1_200],
        }
    )
    sales = pl.DataFrame(
        {
            "source_line_key": ["S1", "S2", "S3"],
            "item_id": ["I1", "I1", "I1"],
            "invoice_date": pl.Series(
                [date(2024, 1, 15), date(2024, 2, 15), date(2024, 3, 15)], dtype=pl.Date
            ),
            "quantity": [5.0, 10.0, 5.0],
        }
    )
    return receipts, sales


def test_fifo_timing_exact() -> None:
    receipts, sales = _world()
    res = cost_sales(receipts, sales, "fifo")
    cogs = {r["source_line_key"]: r["cogs_cents"] for r in res.per_sale.to_dicts()}
    # Jan: 5 x $10. Feb: 5 x $10 + 5 x $12. Mar: 5 x $12.
    assert cogs == {"S1": 5_000, "S2": 11_000, "S3": 6_000}
    ident = res.identity.to_dicts()[0]
    assert ident["receipts_value_cents"] == 22_000
    assert ident["cogs_cents"] + ident["ending_inventory_cents"] == 22_000
    assert ident["ending_inventory_cents"] == 0


def test_average_timing_exact() -> None:
    receipts, sales = _world()
    res = cost_sales(receipts, sales, "average")
    cogs = {r["source_line_key"]: r["cogs_cents"] for r in res.per_sale.to_dicts()}
    # Jan: avg $10 -> 5000. Feb: pool = 5x10 + 10x12 = 17000 over 15 units;
    # COGS = he(17000 x 10 / 15) = he(11333.33) = 11333. Mar: remaining value
    # 5667 over 5 units -> 5667.
    assert cogs == {"S1": 5_000, "S2": 11_333, "S3": 5_667}
    ident = res.identity.to_dicts()[0]
    # Exact value conservation even through rounding.
    assert ident["cogs_cents"] + ident["ending_inventory_cents"] == 22_000


def test_standard_timing_exact() -> None:
    receipts, sales = _world()
    res = cost_sales(receipts, sales, "standard", standard_cost={"I1": 1_100})
    cogs = {r["source_line_key"]: r["cogs_cents"] for r in res.per_sale.to_dicts()}
    # Every unit at the standard $11.00 regardless of receipt timing.
    assert cogs == {"S1": 5_500, "S2": 11_000, "S3": 5_500}


def test_standard_defaults_to_first_receipt() -> None:
    receipts, sales = _world()
    res = cost_sales(receipts, sales, "standard")
    cogs = {r["source_line_key"]: r["cogs_cents"] for r in res.per_sale.to_dicts()}
    assert cogs == {"S1": 5_000, "S2": 10_000, "S3": 5_000}


def test_insufficient_receipts_flagged_never_invented() -> None:
    receipts, sales = _world()
    sales = pl.concat(
        [
            sales,
            pl.DataFrame(
                {
                    "source_line_key": ["S4"],
                    "item_id": ["I1"],
                    "invoice_date": pl.Series([date(2024, 4, 15)], dtype=pl.Date),
                    "quantity": [3.0],
                }
            ),
        ]
    )
    res = cost_sales(receipts, sales, "fifo")
    s4 = res.per_sale.filter(pl.col("source_line_key") == "S4").to_dicts()[0]
    assert s4["cogs_flag"] == "insufficient_receipts"
    assert s4["cogs_cents"] == 3 * 1_200  # last known cost, labeled


def test_scenario_timing_statements() -> None:
    assert "FIFO layers" in scenario_timing_statement("fifo", "new_receipts")
    assert "immediately" in scenario_timing_statement("average", "new_receipts")
    assert "policy" in scenario_timing_statement("standard", "new_receipts")
    assert "revaluation" in scenario_timing_statement("fifo", "existing_pool")
    assert "blend" in scenario_timing_statement("average", "blend")


def test_generated_receipts_cost_the_ledger(tmp_path) -> None:
    """Receipts table from the generator costs the world's sales under all
    three methods with exact value conservation and no shortage flags."""
    from pathlib import Path

    from synth.config import GeneratorConfig, Preset
    from synth.generate import generate

    from margin_engine.config import load_rules
    from margin_engine.ledger.pipeline import run_pipeline

    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=10, n_customers=40, n_items=100)
    generate(cfg, tmp_path)
    res = run_pipeline(
        tmp_path / "erp",
        Path("config/mappings/synth_erp.yaml"),
        load_rules("config/rules.yaml"),
    )
    receipts = res.reference["inventory_receipts"]
    assert receipts is not None
    sales = res.observations.filter(pl.col("quantity_norm") > 0).select(
        "source_line_key", "item_id", "invoice_date", pl.col("quantity").alias("quantity")
    )
    for method in ("fifo", "average", "standard"):
        out = cost_sales(receipts, sales, method)
        assert out.per_sale.height == sales.height
        flagged = out.per_sale.filter(pl.col("cogs_flag") == "insufficient_receipts")
        assert flagged.height == 0, method
        if method in ("fifo", "average"):
            bad = out.identity.filter(
                pl.col("receipts_value_cents")
                != pl.col("cogs_cents") + pl.col("ending_inventory_cents")
            )
            assert bad.height == 0, method


def test_scenario_carries_timing_statement(tmp_path) -> None:
    from datetime import date as _d

    from margin_engine.config import Rules
    from margin_engine.scenarios.tariff import CostShock, run_cost_scenario
    from tests.test_opportunity import _guidance, _panel, _sensitivity

    panel = _panel([{"qty": 10.0}])
    shock = CostShock("t", "category", "CATA", 0.10, _d(2025, 1, 1))
    out = run_cost_scenario(panel, _guidance([{}]), _sensitivity(), shock, Rules(), "fifo")
    assert "cogs_timing" in out.columns
    assert "FIFO layers" in out["cogs_timing"][0]
    assert (out["cost_pool"] == "new_receipts").all()
