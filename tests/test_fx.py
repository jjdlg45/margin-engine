"""Phase 9 verification: multi-currency conversion exact, per-currency
reconciliation, actionable errors, mixed currency + UOM."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.ingest.mapping import MappingError
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.money import div_half_even_expr
from tests.fixtures.builder import FixtureWorld

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def _line(res, key: str) -> dict:
    return res.ledger.filter(pl.col("source_line_key") == key).to_dicts()[0]


def _eur_world(tmp_path: Path, rate: str = "1.25") -> FixtureWorld:
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    w.fx_rates.append({"CURR": "EUR", "PERIOD": "2024-03", "USD_PER_UNIT": rate})
    return w


def test_mixed_currency_fixture_exact(tmp_path: Path) -> None:
    """EUR 100.00 at 1.25 USD/EUR converts to exactly $125.00; USD line
    untouched; per-currency source conservation both exact."""
    w = _eur_world(tmp_path)
    w.add_line(
        INV_NO="INV1",
        CUST_NO="CEUR",
        ITEM_NO="X1",
        QTY_SHP=1,
        UNIT_PRC="100.00",
        EXT_NET="100.00",
        LIST_PRC="100.00",
        UNIT_CST="40.0000",
        CURR="EUR",
        REB_AMT="3.00",
        FRT_CONC="2.00",
    )
    w.add_line(
        INV_NO="INV2",
        CUST_NO="CUSD",
        ITEM_NO="X1",
        QTY_SHP=1,
        UNIT_PRC="90.00",
        EXT_NET="90.00",
        LIST_PRC="100.00",
        UNIT_CST="40.0000",
    )
    res = run_pipeline(w.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    eur = _line(res, "L1")
    assert eur["net_amount_cents"] == 12_500
    assert eur["net_amount_cents_src"] == 10_000
    assert eur["fx_rate_ppm"] == 1_250_000
    # Deductions converted at the same rate: 3.00 EUR -> 3.75 USD; 2.00 -> 2.50.
    assert eur["rebate_actual_cents"] == 375
    assert eur["freight_concession_cents"] == 250
    assert eur["pocket_revenue_cents"] == 12_500 - 375 - 250
    # Cost stays reporting currency: $40.00.
    assert eur["product_cost_cents"] == 4_000
    usd = _line(res, "L2")
    assert usd["net_amount_cents"] == 9_000
    checks = {c["check"]: c for c in res.reconciliation.checks}
    assert checks["net_amount_conservation_EUR"]["expected_cents"] == 10_000
    assert checks["net_amount_conservation_USD"]["expected_cents"] == 9_000


def test_mixed_currency_and_uom_fixture(tmp_path: Path) -> None:
    """24 EA at EUR prices: UOM normalizes to 2 CS AND money converts to USD.
    Gross list from the USD price list x normalized qty stays exact."""
    w = FixtureWorld(tmp_path)
    w.add_item("X1", base_uom="CS")
    w.add_list_price("X1", "01/01/2024", "120.00")
    w.uom_conversions.append({"ITEM_NO": "X1", "FROM_UOM": "EA", "TO_UOM": "CS", "FACTOR": "12"})
    w.fx_rates.append({"CURR": "EUR", "PERIOD": "2024-03", "USD_PER_UNIT": "1.25"})
    w.add_line(
        INV_NO="INV1",
        CUST_NO="CEUR",
        ITEM_NO="X1",
        QTY_SHP=24,
        SELL_UOM="EA",
        UNIT_PRC="7.3334",
        EXT_NET="176.00",
        LIST_PRC="96.00",
        UNIT_CST="60.0000",
        CURR="EUR",
    )
    res = run_pipeline(w.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    row = _line(res, "L1")
    assert row["quantity_norm"] == 2.0
    # EUR 176.00 x 1.25 = $220.00 exactly.
    assert row["invoice_revenue_cents"] == 22_000
    # Gross list: USD list $120.00/CS x 2 CS = $240.00 (reference is USD).
    assert row["gross_list_value_cents"] == 24_000
    # Cost per base unit (CS), USD: 2 x $60.00.
    assert row["product_cost_cents"] == 12_000


def test_missing_fx_rate_actionable(tmp_path: Path) -> None:
    w = _eur_world(tmp_path)
    w.fx_rates.clear()
    w.fx_rates.append({"CURR": "EUR", "PERIOD": "2024-01", "USD_PER_UNIT": "1.25"})
    w.add_line(
        INV_NO="INV1",
        CUST_NO="CEUR",
        ITEM_NO="X1",
        QTY_SHP=1,
        UNIT_PRC="100.00",
        EXT_NET="100.00",
        LIST_PRC="100.00",
        UNIT_CST="40.0000",
        CURR="EUR",
    )
    with pytest.raises(MappingError, match="2024-03"):
        run_pipeline(w.write(), MAPPING, Rules())


def test_foreign_lines_without_fx_table_fail_closed(tmp_path: Path) -> None:
    w = _eur_world(tmp_path)
    w.fx_rates.clear()
    w.add_line(
        INV_NO="INV1",
        CUST_NO="CEUR",
        ITEM_NO="X1",
        QTY_SHP=1,
        UNIT_PRC="100.00",
        EXT_NET="100.00",
        LIST_PRC="100.00",
        UNIT_CST="40.0000",
        CURR="EUR",
    )
    with pytest.raises(MappingError, match="fx_rates"):
        run_pipeline(w.write(), MAPPING, Rules())


def test_generated_multicurrency_world_reconciles(tmp_path: Path) -> None:
    """Generated mixed-currency world: every reconciliation check passes,
    per-currency conservation included, and engine conversions are exactly
    reproducible from the exported amounts and the fx table."""
    cfg = GeneratorConfig.for_preset(
        Preset.WELL_IDENTIFIED,
        seed=5,
        n_customers=80,
        n_items=200,
        currency_shares={"USD": 0.6, "EUR": 0.25, "GBP": 0.15},
    )
    generate(cfg, tmp_path)
    rules = load_rules(REPO / "config" / "rules.yaml")
    res = run_pipeline(tmp_path / "erp", MAPPING, rules)
    assert res.reconciliation.all_pass
    names = {c["check"] for c in res.reconciliation.checks}
    assert {"net_amount_conservation_EUR", "net_amount_conservation_GBP"} <= names
    # Engine conversion self-consistency: converted = half_even(src x ppm / 1e6).
    foreign = res.ledger.filter(pl.col("currency") != "USD")
    assert foreign.height > 0
    recomputed = foreign.select(
        (
            div_half_even_expr(pl.col("net_amount_cents_src") * pl.col("fx_rate_ppm"), 1_000_000)
            - pl.col("net_amount_cents")
        )
        .abs()
        .sum()
        .alias("d")
    ).item()
    assert recomputed == 0
    # Converted totals land near the planted USD world (per-line rounding only).
    import json

    truth = json.loads((tmp_path / "ground_truth" / "truth_summary.json").read_text())
    planted_usd = truth["planted_totals_cents"]["net_cents"]
    # Economic truth excludes verified-duplicate copies, so compare the ledger
    # (already deduped) only; round-trip conversion costs at most ~2c per line.
    engine_usd = int(res.ledger["net_amount_cents"].sum())
    assert abs(engine_usd - planted_usd) <= 2 * res.ledger.height
