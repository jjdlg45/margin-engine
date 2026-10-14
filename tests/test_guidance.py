"""Phase 5 verification: floor math fixtures (to the cent), status assignment,
infeasible corridors flagged, provisional labeling.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.guidance.bands import (
    PROVISIONAL_LABEL,
    STATUS_NO_HEADROOM,
    build_guidance,
    invoice_floor_cents,
    pocket_floor_cents,
)
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.segment.trees import fit_commercial_segments, prepare_segmentation_frame

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


def _floor(cost_cents: int, margin_bp: int) -> int:
    df = pl.DataFrame({"c": [cost_cents]})
    return df.select(pocket_floor_cents(pl.col("c"), margin_bp).alias("f"))["f"][0]


def _inv_floor(cost_cents: int, margin_bp: int, fixed: int, ded_bp: int) -> int:
    df = pl.DataFrame({"c": [cost_cents], "f": [fixed], "d": [ded_bp]})
    return df.select(
        invoice_floor_cents(pl.col("c"), margin_bp, pl.col("f"), pl.col("d")).alias("x")
    )["x"][0]


def test_pocket_floor_spec_example() -> None:
    # Spec: cost $100, m = 5% -> $105.26 pocket floor (100/0.95 = 105.263...).
    assert _floor(10_000, 500) == 10_526


def test_pocket_floor_more_cases() -> None:
    # cost $60, m = 5% -> 60/0.95 = 63.157... -> 63.16
    assert _floor(6_000, 500) == 6_316
    # m = 20% -> 60/0.80 = 75.00 exactly
    assert _floor(6_000, 2_000) == 7_500
    # zero cost -> zero floor
    assert _floor(0, 500) == 0


def test_invoice_floor_formula() -> None:
    # C=$100, m=5%, F=$2.00, d=3% -> (105.26 + 2.00)/0.97 = 110.58
    # exact: (10526.3157 + 200)/0.97 ... integer path: (10526+200)/0.97 = 11057.7 -> 11058
    assert _inv_floor(10_000, 500, 200, 300) == 11_058
    # No deductions: invoice floor == pocket floor.
    assert _inv_floor(10_000, 500, 0, 0) == 10_526


@pytest.fixture(scope="module")
def guided(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    out = tmp_path_factory.mktemp("guide")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=7)
    generate(cfg, out)
    rules = Rules.model_validate({"segmentation": {"bootstrap_resamples": 2}})
    res = run_pipeline(out / "erp", MAPPING, rules)
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    df = prepare_segmentation_frame(flagged, res.reference["customers"], res.reference["items"])
    seg = fit_commercial_segments(df, rules, seed=1)
    df = df.join(seg.leaf_assignments, on="source_line_key", how="left")
    result = build_guidance(df, rules, res.reference["rebate_programs"])
    return result.bands, result.guidance


def test_bands_shape_and_order(guided: tuple[pl.DataFrame, pl.DataFrame]) -> None:
    bands, _ = guided
    assert bands.height >= 2
    ordered = bands.filter(
        (pl.col("band_p25") <= pl.col("band_p50")) & (pl.col("band_p50") <= pl.col("band_p75"))
    )
    assert ordered.height == bands.height


def test_statuses_assigned(guided: tuple[pl.DataFrame, pl.DataFrame]) -> None:
    _, g = guided
    statuses = set(g["guidance_status"].unique().to_list())
    allowed = {
        "in_band",
        "below_reference",
        "below_floor",
        "above_reference",
        "insufficient_pricing_headroom",
        "insufficient_data",
    }
    assert statuses <= allowed
    # The big four must all occur in a realistic world.
    assert {"in_band", "below_reference", "above_reference"} <= statuses


def test_targets_never_below_floor(guided: tuple[pl.DataFrame, pl.DataFrame]) -> None:
    _, g = guided
    with_floor = g.filter(
        pl.col("target_index").is_not_null() & pl.col("floor_index").is_not_null()
    )
    bad = with_floor.filter(
        (pl.col("target_index") < pl.col("floor_index"))
        | (pl.col("start_index") < pl.col("floor_index"))
    )
    assert bad.height == 0


def test_all_targets_provisional(guided: tuple[pl.DataFrame, pl.DataFrame]) -> None:
    _, g = guided
    assert (g["target_label"] == PROVISIONAL_LABEL).all()


def test_infeasible_corridor_flagged_never_relabeled() -> None:
    """A pair whose floor exceeds the band P75 must come out as
    insufficient_pricing_headroom with NO target, not a doctored target."""
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 1}})
    n = 30
    df = pl.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(n)] + ["CX"],
            "item_id": ["I1"] * n + ["I2"],
            "segment_leaf": [1] * (n + 1),
            "price_index": [0.90] * n + [0.85],
            "invoice_date": pl.Series([date(2024, 1, 15)] * (n + 1), dtype=pl.Date),
            "quantity_norm": [1.0] * (n + 1),
            # The CX/I2 pair: cost so high the floor index (cost/(1-m)/list)
            # lands above P75 of the band.
            "product_cost_cents": [5_000] * n + [12_000],
            "eff_list_cents": [10_000] * (n + 1),
            "contract_id": [None] * (n + 1),
            "flag_data_quality_anomaly": [False] * (n + 1),
        }
    )
    result = build_guidance(df, rules, None)
    cx = result.guidance.filter(pl.col("customer_id") == "CX").to_dicts()[0]
    # floor = 12000/0.95 = 12632 cents -> index 1.263 > band p75 (~0.90).
    assert cx["guidance_status"] == STATUS_NO_HEADROOM
    assert cx["target_index"] is None
    assert cx["start_index"] is None


def test_insufficient_data_status() -> None:
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 50}})
    df = pl.DataFrame(
        {
            "customer_id": ["C1", "C2"],
            "item_id": ["I1", "I1"],
            "segment_leaf": [1, 1],
            "price_index": [0.9, 0.92],
            "invoice_date": pl.Series([date(2024, 1, 15)] * 2, dtype=pl.Date),
            "quantity_norm": [1.0, 1.0],
            "product_cost_cents": [5_000, 5_000],
            "eff_list_cents": [10_000, 10_000],
            "contract_id": [None, None],
            "flag_data_quality_anomaly": [False, False],
        }
    )
    result = build_guidance(df, rules, None)
    assert (result.guidance["guidance_status"] == "insufficient_data").all()
    assert result.guidance["target_index"].is_null().all()


def test_data_quality_lines_excluded_from_bands() -> None:
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 1}})
    df = pl.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(10)],
            "item_id": ["I1"] * 10,
            "segment_leaf": [1] * 10,
            "price_index": [0.9] * 9 + [9.0],  # entry error would wreck p75
            "invoice_date": pl.Series([date(2024, 1, 15)] * 10, dtype=pl.Date),
            "quantity_norm": [1.0] * 10,
            "product_cost_cents": [5_000] * 10,
            "eff_list_cents": [10_000] * 10,
            "contract_id": [None] * 10,
            "flag_data_quality_anomaly": [False] * 9 + [True],
        }
    )
    result = build_guidance(df, rules, None)
    assert result.bands["band_p75"][0] == pytest.approx(0.9)
