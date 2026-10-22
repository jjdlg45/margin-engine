"""Phase 16 verification: planted win-curve coefficient recovered within ±15%
relative; negotiation gap recovered; win-aware optimization behaves."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import load_rules
from margin_engine.extended.quotes import (
    STATUS_ESTIMATED,
    fit_win_curves,
    optimal_price_win_aware,
    quote_price_index,
)
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PipelineResult, pl.DataFrame]:
    out = tmp_path_factory.mktemp("quotes")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=2, n_customers=200, n_items=400)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, load_rules(REPO / "config" / "rules.yaml"))
    panel_truth = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    seg_map = panel_truth.select(
        "customer_id", "item_id", pl.col("segment_id").alias("segment_leaf")
    ).unique()
    return out, res, seg_map


def test_win_curve_every_estimated_segment_within_15pct(
    world: tuple[Path, PipelineResult, pl.DataFrame],
) -> None:
    """EVERY segment carrying estimated_win_curve status recovers the planted
    coefficient within +/-15% relative -- not just the median. Segments whose
    fit cannot support that accuracy must carry a lower status (fail closed):
    insufficient_data (too few quotes) or insufficient_precision (relative SE
    above the configured maximum)."""
    out, res, seg_map = world
    quotes = res.reference["quotes"]
    assert quotes is not None and quotes.height > 3_000
    qi = quote_price_index(quotes, res.reference["price_list"])
    curves = fit_win_curves(qi, seg_map)
    truth = json.loads((out / "ground_truth" / "truth_summary.json").read_text())["quotes"][
        "win_curve"
    ]
    counts: dict[str, int] = {}
    failures = []
    rel_errs = []
    for r in curves.to_dicts():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] != STATUS_ESTIMATED:
            continue
        b_true = truth[str(r["segment"])]["b"]
        rel = abs(r["price_coef"] - b_true) / abs(b_true)
        rel_errs.append(rel)
        print(
            f"seg {r['segment']}: n={r['n_quotes']} coef={r['price_coef']:.2f} "
            f"true={b_true:.2f} rel_err={rel:.3f}"
        )
        if rel > 0.15:
            failures.append((r["segment"], rel))
    print(f"status counts: {counts}")
    assert counts.get(STATUS_ESTIMATED, 0) >= 2, f"too few estimated segments: {counts}"
    assert not failures, f"estimated segments over +/-15% relative error: {failures}"
    print(
        f"win-curve relative errors: median={float(np.median(rel_errs)):.3f} worst={max(rel_errs):.3f}"
    )


def test_win_curve_precision_gate_fails_closed(
    world: tuple[Path, PipelineResult, pl.DataFrame],
) -> None:
    """A fit whose relative SE exceeds the maximum is insufficient_precision:
    coefficient, SE and CI stay reported (honest), status keeps it out of
    gating and optimization. The gate binds on this world."""
    from margin_engine.extended.quotes import STATUS_LOW_PRECISION

    _, res, seg_map = world
    qi = quote_price_index(res.reference["quotes"], res.reference["price_list"])
    curves = fit_win_curves(qi, seg_map)
    low = curves.filter(pl.col("status") == STATUS_LOW_PRECISION)
    assert low.height >= 1
    for r in low.to_dicts():
        assert r["price_coef"] is not None and r["price_coef_se"] is not None
        assert r["price_coef_se"] / abs(r["price_coef"]) > 0.10
        assert "relative SE" in r["reasons"]
    # Estimated segments all satisfy the gate.
    for r in curves.filter(pl.col("status") == STATUS_ESTIMATED).to_dicts():
        assert r["price_coef_se"] / abs(r["price_coef"]) <= 0.10


def test_negotiation_gap_recovered(world: tuple[Path, PipelineResult, pl.DataFrame]) -> None:
    out, res, seg_map = world
    qi = quote_price_index(res.reference["quotes"], res.reference["price_list"])
    curves = fit_win_curves(qi, seg_map)
    truth_gap = json.loads((out / "ground_truth" / "truth_summary.json").read_text())["quotes"][
        "negotiation_gap_mean"
    ]
    gaps = [r["negotiation_gap"] for r in curves.to_dicts() if r["negotiation_gap"] is not None]
    assert gaps
    assert abs(float(np.mean(gaps)) - truth_gap) < 0.01


def test_uncertainty_and_status_reported(world: tuple[Path, PipelineResult, pl.DataFrame]) -> None:
    _, res, seg_map = world
    qi = quote_price_index(res.reference["quotes"], res.reference["price_list"])
    curves = fit_win_curves(qi, seg_map)
    for r in curves.to_dicts():
        if r["status"] == STATUS_ESTIMATED:
            assert r["price_coef_se"] is not None and r["price_coef_se"] > 0
            assert r["ci_low"] < r["price_coef"] < r["ci_high"]
        else:
            assert r["reasons"]


def test_insufficient_quotes_fail_closed() -> None:
    qi = pl.DataFrame(
        {
            "customer_id": ["C1"] * 10,
            "item_id": ["I1"] * 10,
            "quote_index": [0.9] * 10,
            "won": [True, False] * 5,
            "close_price_cents": [None] * 10,
            "quoted_price_cents": [9_000] * 10,
        }
    )
    seg = pl.DataFrame({"customer_id": ["C1"], "item_id": ["I1"], "segment_leaf": [1]})
    curves = fit_win_curves(qi, seg)
    assert (curves["status"] == "insufficient_data").all()


def test_win_aware_optimum_below_monopoly() -> None:
    """A falling win curve pulls the optimum below the flat-curve optimum,
    and a steeper curve pulls it lower still."""
    base = {
        "cost_cents": 6_000,
        "p0_cents": 9_000,
        "list_cents": 10_000,
        "floor_cents": 6_500,
    }
    p_flat, _ = optimal_price_win_aware(win_intercept=50.0, win_price_coef=-0.001, **base)
    p_soft, _ = optimal_price_win_aware(win_intercept=5.4, win_price_coef=-6.0, **base)
    p_steep, _ = optimal_price_win_aware(win_intercept=9.0, win_price_coef=-10.0, **base)
    assert p_soft < p_flat
    assert p_steep < p_soft
    assert p_soft >= base["floor_cents"]


def test_win_aware_uses_single_volume_response() -> None:
    """24.13: the win-aware optimizer accepts NO elasticity - one volume
    response source per record is enforced by the signature itself."""
    import inspect

    params = inspect.signature(optimal_price_win_aware).parameters
    assert "elasticity" not in params
    with pytest.raises(TypeError):
        optimal_price_win_aware(
            cost_cents=6_000,
            elasticity=-1.6,  # type: ignore[call-arg]
            p0_cents=9_000,
            win_intercept=5.4,
            win_price_coef=-6.0,
            list_cents=10_000,
            floor_cents=6_500,
        )
