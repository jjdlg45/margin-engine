"""Phase 37: pricing compliance.

- Robinson-Patman review flag fires strictly above the threshold, never at the
  boundary, and never for non-competing customers.
- The cost-to-serve evidence beside the flag matches the ledger.
- The market price index control is refused without a declared public source.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import polars as pl
import yaml

from margin_engine.compliance import run_robinson_patman
from margin_engine.config import load_rules

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample" / "erp"
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"
RULES = REPO / "config" / "rules.yaml"


def _guidance(prices: dict[str, int], item: str = "I1") -> pl.DataFrame:
    # Recommended price = target_index x eff_list; use index=price/10000, list=10000.
    return pl.DataFrame(
        {
            "customer_id": list(prices),
            "item_id": [item] * len(prices),
            "target_index": [p / 10_000 for p in prices.values()],
            "eff_list_cents": [10_000] * len(prices),
        }
    )


def _customers(region: dict[str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "customer_id": list(region),
            "industry": ["construction"] * len(region),
            "region": list(region.values()),
        }
    )


def test_flag_fires_strictly_above_threshold_boundary() -> None:
    rules = load_rules(RULES)  # threshold 5.0%
    # B is exactly 5.00% above A (boundary: no flag); C is above 5% (flag).
    guidance = _guidance({"A": 10_000, "B": 10_500, "C": 10_501})
    customers = _customers({"A": "west", "B": "west", "C": "west"})
    res = run_robinson_patman(guidance, customers, None, rules)
    pairs = {(r["customer_id_a"], r["customer_id_b"]) for r in res.detail.to_dicts()}
    assert ("A", "B") not in pairs  # exactly at the boundary: not flagged
    assert ("A", "C") in pairs  # 5.01% apart: flagged
    flagged_customers = set(res.flags["customer_id"].to_list())
    assert "A" in flagged_customers and "C" in flagged_customers


def test_no_flag_for_non_competing_customers() -> None:
    rules = load_rules(RULES)
    guidance = _guidance({"A": 10_000, "B": 20_000})  # 100% apart
    customers = _customers({"A": "west", "B": "east"})  # different region
    res = run_robinson_patman(guidance, customers, None, rules)
    assert res.detail.height == 0
    assert res.flags.height == 0


def test_cts_evidence_matches_ledger() -> None:
    """The cost-to-serve numbers shown beside the flag are the pair-level
    per-unit figures derived from the ledger's activity costs — recomputed here
    from a mini-ledger exactly as the pipeline derives them."""
    rules = load_rules(RULES)
    ledger = pl.DataFrame(
        {
            "customer_id": ["A", "A", "B"],
            "item_id": ["I1", "I1", "I1"],
            "transaction_type": ["sale"] * 3,
            "quantity_norm": [2.0, 2.0, 4.0],
            "activity_cts_cents": [300, 100, 200],
        }
    )
    cts_per_unit = (
        ledger.filter(
            (pl.col("transaction_type") == "sale")
            & (pl.col("quantity_norm") > 0)
            & pl.col("activity_cts_cents").is_not_null()
        )
        .group_by("customer_id", "item_id")
        .agg(
            pl.col("activity_cts_cents").sum().alias("_cts"),
            pl.col("quantity_norm").sum().alias("_qty"),
        )
        .with_columns(
            (pl.col("_cts") / pl.col("_qty")).round(0).cast(pl.Int64).alias("cts_per_unit_cents")
        )
        .select("customer_id", "item_id", "cts_per_unit_cents")
    )
    # Ledger says: A = 400 cents / 4 units = 100; B = 200 / 4 = 50.
    guidance = _guidance({"A": 10_000, "B": 12_000})
    customers = _customers({"A": "west", "B": "west"})
    res = run_robinson_patman(guidance, customers, cts_per_unit, rules)
    assert res.detail.height == 1
    row = res.detail.to_dicts()[0]
    assert row["cts_per_unit_a_cents"] == 100
    assert row["cts_per_unit_b_cents"] == 50
    assert row["cts_difference_cents"] == 50


def test_disabled_and_missing_attributes_fail_closed() -> None:
    rules = load_rules(RULES)
    guidance = _guidance({"A": 10_000, "B": 20_000})
    no_attrs = pl.DataFrame({"customer_id": ["A", "B"]})
    res = run_robinson_patman(guidance, no_attrs, None, rules)
    assert res.detail.height == 0
    assert any("lacks competitor attribute" in n for n in res.notes)
    res2 = run_robinson_patman(guidance, None, None, rules)
    assert any("no customers reference table" in n for n in res2.notes)


def test_market_index_requires_declared_public_source(tmp_path: Path) -> None:
    """A mapped market price index without public_source: true is refused as an
    elasticity control, with the reason stated; declaring it public uses it."""
    from margin_engine.report.full_run import run_full

    world = tmp_path / "erp"
    shutil.copytree(SAMPLE, world)
    # A category-level index matching the synth mapping's columns.
    items = pl.read_csv(world / "items.csv", infer_schema=False)
    cat_col = "CATEGORY" if "CATEGORY" in items.columns else items.columns[0]
    cats = items[cat_col].unique().head(2).to_list()
    pl.DataFrame(
        {
            "CATEGORY": cats * 2,
            "PERIOD": ["2024-01"] * len(cats) + ["2024-02"] * len(cats),
            "MKT_IDX": ["1.00"] * len(cats) + ["1.02"] * len(cats),
        }
    ).write_csv(world / "market_price_index.csv")

    mapping_raw = yaml.safe_load(MAPPING.read_text(encoding="utf-8"))
    undeclared = tmp_path / "mapping_undeclared.yaml"
    mapping_raw["reference_tables"]["market_price_index"].pop("public_source", None)
    undeclared.write_text(yaml.safe_dump(mapping_raw), encoding="utf-8")

    res = run_full(world, undeclared, RULES, tmp_path / "out1", validate=False)
    assert any("NOT used as an elasticity control" in n for n in res.pipeline.notes)

    declared = tmp_path / "mapping_declared.yaml"
    mapping_raw["reference_tables"]["market_price_index"]["public_source"] = True
    declared.write_text(yaml.safe_dump(mapping_raw), encoding="utf-8")
    res2 = run_full(world, declared, RULES, tmp_path / "out2", validate=False)
    assert not any("NOT used as an elasticity control" in n for n in res2.pipeline.notes)
