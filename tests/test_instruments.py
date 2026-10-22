"""Run 4 (27.6): instrument builder. The receipts-built supplier cost index
recovers the planted supplier cost shocks; the external option maps public
series to suppliers; every failure mode is an actionable error."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules
from margin_engine.ingest.mapping import MappingError
from margin_engine.sensitivity.instruments import (
    build_supplier_cost_index,
    external_cost_index,
)

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


def test_hand_calc_receipts_index() -> None:
    """Two suppliers, two months each: cost_idx is exp(demeaned log avg cost)."""
    receipts = pl.DataFrame(
        {
            "item_id": ["I1", "I1", "I2", "I2"],
            "receipt_date": pl.Series(
                [date(2024, 1, 5), date(2024, 2, 5), date(2024, 1, 7), date(2024, 2, 7)],
                dtype=pl.Date,
            ),
            "quantity": [10, 10, 5, 5],
            "unit_cost_cents": [1_000, 1_210, 2_000, 2_000],
        }
    )
    items = pl.DataFrame({"item_id": ["I1", "I2"], "supplier_id": ["S1", "S2"]})
    idx, _notes = build_supplier_cost_index(receipts, items)
    s1 = {r["period"]: r["cost_idx"] for r in idx.filter(pl.col("supplier_id") == "S1").to_dicts()}
    # log costs: ln(1000), ln(1210); demeaned: -+0.5*ln(1.21) -> idx = 1.1^(-+0.5)... exact:
    half_log = (np.log(1210) - np.log(1000)) / 2
    assert s1["2024-01"] == pytest.approx(float(np.exp(-half_log)))
    assert s1["2024-02"] == pytest.approx(float(np.exp(half_log)))
    s2 = {r["period"]: r["cost_idx"] for r in idx.filter(pl.col("supplier_id") == "S2").to_dicts()}
    assert s2["2024-01"] == pytest.approx(1.0)  # constant cost -> flat index


def test_gaps_forward_filled_and_noted() -> None:
    receipts = pl.DataFrame(
        {
            "item_id": ["I1", "I1"],
            "receipt_date": pl.Series([date(2024, 1, 5), date(2024, 3, 5)], dtype=pl.Date),
            "quantity": [10, 10],
            "unit_cost_cents": [1_000, 1_000],
        }
    )
    # A second supplier defines the 2024-02 period in the grid.
    receipts = pl.concat(
        [
            receipts,
            pl.DataFrame(
                {
                    "item_id": ["I2"],
                    "receipt_date": pl.Series([date(2024, 2, 5)], dtype=pl.Date),
                    "quantity": [1],
                    "unit_cost_cents": [500],
                }
            ),
        ]
    )
    items = pl.DataFrame({"item_id": ["I1", "I2"], "supplier_id": ["S1", "S2"]})
    idx, notes = build_supplier_cost_index(receipts, items)
    s1 = idx.filter(pl.col("supplier_id") == "S1")
    assert "2024-02" in s1["period"].to_list()  # gap filled, not dropped
    assert any("carried the last observed cost forward" in n for n in notes)


def test_receipts_index_recovers_planted_shocks(tmp_path: Path) -> None:
    """On a generated world, the receipts-built index must track the planted
    supplier cost shocks (receipts are written at planted unit costs)."""
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=9, n_customers=20, n_items=60)
    generate(cfg, tmp_path / "w")
    from margin_engine.ledger.pipeline import run_pipeline

    res = run_pipeline(tmp_path / "w" / "erp", MAPPING, Rules())
    built, _ = build_supplier_cost_index(
        res.reference["inventory_receipts"], res.reference["items"]
    )
    planted = res.reference["supplier_cost_index"].with_columns(
        pl.col("cost_idx").log().alias("z_planted")
    )
    joined = built.with_columns(pl.col("cost_idx").log().alias("z_built")).join(
        planted.select("supplier_id", "period", "z_planted"),
        on=["supplier_id", "period"],
        how="inner",
    )
    assert joined.height > 100
    corr = float(np.corrcoef(joined["z_built"].to_numpy(), joined["z_planted"].to_numpy())[0, 1])
    assert corr > 0.9, f"receipts-built index must track planted shocks, got r={corr:.3f}"


def test_external_index_maps_series_to_suppliers() -> None:
    series = pl.DataFrame(
        {
            "series_id": ["PPI_A", "PPI_A", "PPI_B", "PPI_B"],
            "period": ["2024-01", "2024-02", "2024-01", "2024-02"],
            "value": [100.0, 110.0, 200.0, 200.0],
        }
    )
    items = pl.DataFrame(
        {
            "item_id": ["I1", "I2", "I3"],
            "supplier_id": ["S1", "S1", "S2"],
            "item_category": ["CATA", "CATA", "CATB"],
        }
    )
    idx, notes = external_cost_index(series, {"CATA": "PPI_A", "CATB": "PPI_B"}, items)
    s1 = {r["period"]: r["cost_idx"] for r in idx.filter(pl.col("supplier_id") == "S1").to_dicts()}
    half_log = (np.log(110) - np.log(100)) / 2
    assert s1["2024-02"] == pytest.approx(float(np.exp(half_log)))
    s2 = {r["period"]: r["cost_idx"] for r in idx.filter(pl.col("supplier_id") == "S2").to_dicts()}
    assert s2["2024-01"] == pytest.approx(1.0)
    assert any("public" in n for n in notes)


def test_actionable_errors() -> None:
    items = pl.DataFrame({"item_id": ["I1"], "supplier_id": ["S1"], "item_category": ["CATA"]})
    with pytest.raises(MappingError, match="inventory_receipts lacks"):
        build_supplier_cost_index(pl.DataFrame({"item_id": ["I1"]}), items)
    series = pl.DataFrame({"series_id": ["X"], "period": ["2024-01"], "value": [1.0]})
    with pytest.raises(MappingError, match="external_index_map is empty"):
        external_cost_index(series, {}, items)
    with pytest.raises(MappingError, match="no series in"):
        external_cost_index(series, {"OTHER": "X"}, items)
