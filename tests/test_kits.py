"""Phase 12 verification: kit allocation exact, free goods classified and
excluded from dispersion."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline
from tests.fixtures.builder import FixtureWorld

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def test_kit_allocation_relative_standalone_exact(tmp_path: Path) -> None:
    """Kit $100.00 with components listed $30x2=$60 and $40x1=$40: allocation
    60/40 -> $60.00 and $40.00; parent zeroed; invoice revenue conserved."""
    w = FixtureWorld(tmp_path)
    w.add_item("KITP")
    w.add_item("CA")
    w.add_item("CB")
    w.add_list_price("KITP", "01/01/2024", "110.00")
    w.add_list_price("CA", "01/01/2024", "30.00")
    w.add_list_price("CB", "01/01/2024", "40.00")
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="KITP",
        QTY_SHP=1,
        UNIT_PRC="100.00",
        EXT_NET="100.00",
        LIST_PRC="110.00",
        UNIT_CST="50.0000",
        KIT_ID="KIT1",
    )
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="CA",
        QTY_SHP=2,
        UNIT_PRC="0",
        EXT_NET="0.00",
        LIST_PRC="30.00",
        UNIT_CST="12.0000",
        KIT_ID="KIT1",
    )
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="CB",
        QTY_SHP=1,
        UNIT_PRC="0",
        EXT_NET="0.00",
        LIST_PRC="40.00",
        UNIT_CST="18.0000",
        KIT_ID="KIT1",
    )
    res = run_pipeline(w.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    rows = {r["source_line_key"]: r for r in res.ledger.to_dicts()}
    assert rows["L1"]["kit_role"] == "kit_parent"
    assert rows["L1"]["kit_revenue_realloc_cents"] == -10_000
    assert rows["L1"]["invoice_revenue_cents"] == 0
    assert rows["L2"]["kit_role"] == "kit_component"
    assert rows["L2"]["kit_revenue_realloc_cents"] == 6_000
    assert rows["L3"]["kit_revenue_realloc_cents"] == 4_000
    total = sum(rows[k]["invoice_revenue_cents"] for k in ("L1", "L2", "L3"))
    assert total == 10_000


def test_kit_allocation_rounding_sums_exactly(tmp_path: Path) -> None:
    """Non-divisible split (lists $10 / $10 / $10 over $100.01 kit revenue, 3
    components): Hamilton keeps the sum exact."""
    w = FixtureWorld(tmp_path)
    w.add_item("KITP")
    w.add_list_price("KITP", "01/01/2024", "110.00")
    for c in ("CA", "CB", "CC"):
        w.add_item(c)
        w.add_list_price(c, "01/01/2024", "10.00")
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="KITP",
        QTY_SHP=1,
        UNIT_PRC="100.01",
        EXT_NET="100.01",
        LIST_PRC="110.00",
        UNIT_CST="50.0000",
        KIT_ID="KIT1",
    )
    for c in ("CA", "CB", "CC"):
        w.add_line(
            INV_NO="INV1",
            CUST_NO="C1",
            ITEM_NO=c,
            QTY_SHP=1,
            UNIT_PRC="0",
            EXT_NET="0.00",
            LIST_PRC="10.00",
            UNIT_CST="4.0000",
            KIT_ID="KIT1",
        )
    res = run_pipeline(w.write(), MAPPING, Rules())
    comps = res.ledger.filter(pl.col("kit_role") == "kit_component")
    assert int(comps["kit_revenue_realloc_cents"].sum()) == 10_001
    assert sorted(comps["kit_revenue_realloc_cents"].to_list()) == [3_333, 3_334, 3_334]


def test_free_goods_classified_own_type(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "50.00")
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=10,
        UNIT_PRC="50.00",
        EXT_NET="500.00",
        LIST_PRC="50.00",
        UNIT_CST="20.0000",
    )
    w.add_line(
        INV_NO="INV1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=2,
        UNIT_PRC="0",
        EXT_NET="0.00",
        LIST_PRC="50.00",
        UNIT_CST="20.0000",
        TXN_CD="F",
    )
    res = run_pipeline(w.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    free = res.ledger.filter(pl.col("transaction_type") == "free_good")
    assert free.height == 1
    # Own type: not in observations, zero pocket revenue, real cost kept.
    assert res.observations.filter(pl.col("transaction_type") == "free_good").height == 0
    row = free.to_dicts()[0]
    assert row["pocket_revenue_cents"] == 0
    assert row["product_cost_cents"] == 4_000


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PipelineResult]:
    out = tmp_path_factory.mktemp("kits")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=6, n_customers=80, n_items=200)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, load_rules(REPO / "config" / "rules.yaml"))
    return out, res


def test_generated_kits_allocate_exactly(generated: tuple[Path, PipelineResult]) -> None:
    _, res = generated
    kits = res.ledger.filter(pl.col("kit_role").is_not_null())
    assert kits.height > 50
    per_kit = kits.group_by("invoice_id", "kit_parent_id").agg(
        pl.col("kit_revenue_realloc_cents").sum().alias("net_realloc"),
        (pl.col("kit_role") == "kit_component").sum().alias("n_comp"),
    )
    with_comps = per_kit.filter(pl.col("n_comp") > 0)
    assert with_comps.height > 10
    assert with_comps.filter(pl.col("net_realloc") != 0).height == 0
    assert res.reconciliation.all_pass


def test_free_goods_never_price_outliers(generated: tuple[Path, PipelineResult]) -> None:
    _, res = generated
    free = res.ledger.filter(pl.col("transaction_type") == "free_good")
    assert free.height > 10
    rules = load_rules(REPO / "config" / "rules.yaml")
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    j = flagged.join(free.select("source_line_key"), on="source_line_key", how="semi")
    assert j.height == 0  # free goods never reach dispersion at all
