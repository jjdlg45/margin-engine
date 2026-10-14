"""Phase 3 verification: planted outlier recovery, MAD-zero fallback,
flag separation, ABC splits.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules
from margin_engine.dispersion.analytics import (
    abc_classification,
    flag_outliers,
    run_dispersion,
)
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


@pytest.fixture(scope="module")
def flagged_world(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[pl.DataFrame, pl.DataFrame, PipelineResult, Path]:
    out = tmp_path_factory.mktemp("disp")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=5, n_customers=100, n_items=300)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, Rules())
    flagged, stats, _abc = run_dispersion(res.observations, res.reference["items"], Rules())
    return flagged, stats, res, out


def _with_truth(flagged: pl.DataFrame, out: Path) -> pl.DataFrame:
    truth = pl.read_parquet(out / "ground_truth" / "line_truth.parquet")
    return flagged.join(
        truth.select("line_key", "truth_outlier_type"),
        left_on="source_line_key",
        right_on="line_key",
        how="left",
    )


def test_planted_outlier_recall_and_false_flag_rate(
    flagged_world: tuple[pl.DataFrame, pl.DataFrame, PipelineResult, Path],
) -> None:
    flagged, _, _, out = flagged_world
    j = _with_truth(flagged, out)
    any_flag = pl.col("flag_data_quality_anomaly") | pl.col("flag_commercial_pricing_outlier")

    planted = j.filter(pl.col("truth_outlier_type").is_in(["data_quality", "commercial"]))
    detected = planted.filter(any_flag)
    recall = detected.height / planted.height
    assert recall >= 0.90, f"planted outlier recall {recall:.2%} < 90%"

    clean = j.filter(pl.col("truth_outlier_type").is_null() | (pl.col("truth_outlier_type") == ""))
    false_flagged = clean.filter(any_flag)
    fpr = false_flagged.height / clean.height
    assert fpr <= 0.05, f"false-flag rate {fpr:.2%} > 5%"


def test_data_quality_outliers_classified_correctly(
    flagged_world: tuple[pl.DataFrame, pl.DataFrame, PipelineResult, Path],
) -> None:
    flagged, _, _, out = flagged_world
    j = _with_truth(flagged, out)
    dq = j.filter(pl.col("truth_outlier_type") == "data_quality")
    got = dq.filter(pl.col("flag_data_quality_anomaly"))
    assert got.height / dq.height >= 0.90
    # Detected data-quality lines must not be labeled commercial.
    assert not dq.filter(pl.col("flag_data_quality_anomaly"))[
        "flag_commercial_pricing_outlier"
    ].any()


def test_flags_never_mixed(
    flagged_world: tuple[pl.DataFrame, pl.DataFrame, PipelineResult, Path],
) -> None:
    flagged, _, _, _ = flagged_world
    both = flagged.filter(
        pl.col("flag_data_quality_anomaly") & pl.col("flag_commercial_pricing_outlier")
    )
    assert both.height == 0


def test_originals_preserved(
    flagged_world: tuple[pl.DataFrame, pl.DataFrame, PipelineResult, Path],
) -> None:
    flagged, _, res, _ = flagged_world
    # Flagging adds columns; it never drops or mutates rows.
    assert flagged.height == res.observations.height
    assert flagged["net_amount_cents"].sum() == res.observations["net_amount_cents"].sum()


def test_price_index_method_labeled(
    flagged_world: tuple[pl.DataFrame, pl.DataFrame, PipelineResult, Path],
) -> None:
    flagged, _, _, _ = flagged_world
    methods = set(flagged["price_index_method"].unique().to_list())
    assert methods <= {"price_vs_effective_list", "price_vs_item_median", "unavailable"}
    # Most lines should normalize against the effective list.
    share_list = (
        flagged.filter(pl.col("price_index_method") == "price_vs_effective_list").height
        / flagged.height
    )
    assert share_list > 0.95


def test_mad_zero_fallback() -> None:
    """A degenerate peer group (identical prices + one deviant) has MAD = 0:
    the modal-price fallback must flag the deviant and label the method."""
    n = 40
    df = pl.DataFrame(
        {
            "price_index": [1.0] * (n - 1) + [1.2],
            "peer_group": ["G"] * n,
            "peer_group_level": ["item"] * n,
        }
    )
    out = flag_outliers(df, Rules())
    assert out.filter(pl.col("outlier_method") == "mad_zero_modal_fallback").height == n
    deviant = out.filter(pl.col("price_index") == 1.2)
    assert deviant["flag_commercial_pricing_outlier"].all()
    normal = out.filter(pl.col("price_index") == 1.0)
    assert not normal["flag_commercial_pricing_outlier"].any()
    assert not normal["flag_data_quality_anomaly"].any()


def test_mad_zero_fallback_entry_error_classified_dq() -> None:
    n = 40
    df = pl.DataFrame(
        {
            "price_index": [1.0] * (n - 1) + [10.0],
            "peer_group": ["G"] * n,
            "peer_group_level": ["item"] * n,
        }
    )
    out = flag_outliers(df, Rules())
    deviant = out.filter(pl.col("price_index") == 10.0)
    assert deviant["flag_data_quality_anomaly"].all()
    assert not deviant["flag_commercial_pricing_outlier"].any()


def test_small_cohort_not_flagged() -> None:
    df = pl.DataFrame(
        {
            "price_index": [1.0, 1.0, 9.0],
            "peer_group": ["G"] * 3,
            "peer_group_level": ["item"] * 3,
        }
    )
    out = flag_outliers(df, Rules())
    assert (out["outlier_method"] == "cohort_too_small").all()
    assert not out["flag_data_quality_anomaly"].any()


def test_abc_three_versions(
    flagged_world: tuple[pl.DataFrame, pl.DataFrame, PipelineResult, Path],
) -> None:
    _, _, res, _ = flagged_world
    abc = abc_classification(res.observations, Rules())
    assert set(abc.columns) == {"item_id", "revenue_abc", "unit_abc", "frequency_abc"}
    for col in ("revenue_abc", "unit_abc", "frequency_abc"):
        assert set(abc[col].unique().to_list()) <= {"A", "B", "C"}
    # Revenue ABC: A items carry ~80% of revenue.
    rev = res.observations.group_by("item_id").agg(pl.col("net_amount_cents").sum())
    j = rev.join(abc, on="item_id")
    share_a = (
        j.filter(pl.col("revenue_abc") == "A")["net_amount_cents"].sum()
        / j["net_amount_cents"].sum()
    )
    assert 0.70 <= share_a <= 0.85
    # The three versions genuinely differ somewhere.
    diff = abc.filter(
        (pl.col("revenue_abc") != pl.col("unit_abc"))
        | (pl.col("revenue_abc") != pl.col("frequency_abc"))
    )
    assert diff.height > 0


def test_peer_stats_shape(
    flagged_world: tuple[pl.DataFrame, pl.DataFrame, PipelineResult, Path],
) -> None:
    _, stats, _, _ = flagged_world
    assert {"median", "iqr", "p10", "p50", "p90", "mean_discount_to_list"} <= set(stats.columns)
    assert stats.height > 0
    sane = stats.filter((pl.col("p10") <= pl.col("p50")) & (pl.col("p50") <= pl.col("p90")))
    assert sane.height == stats.height
