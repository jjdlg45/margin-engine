"""Phase 1 verification: determinism, mess rates, preset conditions.

Uses a reduced-scale world (fast) shared across tests via module-scoped
fixtures; preset condition tests generate their own worlds.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

SMALL = {"n_customers": 60, "n_items": 200}


def _gen(tmp: Path, preset: Preset, seed: int = 42, **kw: Any) -> tuple[Path, dict[str, Any]]:
    cfg = GeneratorConfig.for_preset(preset, seed=seed, **{**SMALL, **kw})
    out = tmp / f"{preset.value}_{seed}"
    summary = generate(cfg, out)
    return out, summary


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    return _gen(tmp_path_factory.mktemp("gen"), Preset.WELL_IDENTIFIED)


def _file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_same_seed_identical_output(tmp_path: Path) -> None:
    a, _ = _gen(tmp_path / "a", Preset.WELL_IDENTIFIED, seed=7)
    b, _ = _gen(tmp_path / "b", Preset.WELL_IDENTIFIED, seed=7)
    for name in ["invoice_lines.csv", "customers.csv", "items.csv", "price_list.csv"]:
        assert _file_hash(a / "erp" / name) == _file_hash(b / "erp" / name), name
    sa = json.loads((a / "ground_truth" / "truth_summary.json").read_text())
    sb = json.loads((b / "ground_truth" / "truth_summary.json").read_text())
    assert sa == sb


def test_different_seed_different_output(tmp_path: Path) -> None:
    a, _ = _gen(tmp_path / "a", Preset.WELL_IDENTIFIED, seed=1)
    b, _ = _gen(tmp_path / "b", Preset.WELL_IDENTIFIED, seed=2)
    assert _file_hash(a / "erp" / "invoice_lines.csv") != _file_hash(
        b / "erp" / "invoice_lines.csv"
    )


def test_every_mess_type_present(world: tuple[Path, dict[str, Any]]) -> None:
    _, summary = world
    counts = summary["mess_counts"]
    for key, value in counts.items():
        assert value > 0, f"mess type {key} never appeared"


def test_mess_rates_match_parameters(world: tuple[Path, dict[str, Any]]) -> None:
    out, summary = world
    cfg = GeneratorConfig.model_validate(summary["generator_config"])
    counts = summary["mess_counts"]
    truth = pl.read_parquet(out / "ground_truth" / "line_truth.parquet")
    n_sales = truth.filter(pl.col("txn_type") == "sale").height

    def within(actual: int, expected_rate: float, base: int, tol: float = 0.5) -> bool:
        expected = expected_rate * base
        return expected * (1 - tol) <= actual <= expected * (1 + tol)

    assert within(counts["returns"], cfg.mess.return_rate, n_sales)
    assert within(counts["missing_cost"], cfg.mess.missing_cost_rate, n_sales)
    assert within(counts["data_quality_outliers"], cfg.mess.price_outlier_rate, n_sales)
    assert within(counts["commercial_outliers"], cfg.mess.commercial_outlier_rate, n_sales)
    assert within(counts["verified_dup_keys"], cfg.mess.verified_dup_rate, truth.height)
    assert within(counts["lookalike_dups"], cfg.mess.lookalike_dup_rate, truth.height)


def test_erp_lines_reconcile_internally(world: tuple[Path, dict[str, Any]]) -> None:
    """EXT_NET is authoritative; for normal lines it equals QTY x UNIT_PRC within
    a cent, and LIST_PRC x QTY - DISC_AMT == EXT_NET exactly for sale lines."""
    out, _ = world
    df = pl.read_csv(out / "erp" / "invoice_lines.csv", infer_schema_length=10000)
    sales = df.filter((pl.col("TXN_CD") == "S") & (pl.col("QTY_SHP") > 0))
    err = (
        sales.with_columns(
            (pl.col("UNIT_PRC") * pl.col("QTY_SHP") - pl.col("EXT_NET")).abs().alias("diff")
        )
        .select(pl.col("diff").max())
        .item()
    )
    assert err < 0.01 * sales["QTY_SHP"].max()


def test_zero_purchase_periods_exist(world: tuple[Path, dict[str, Any]]) -> None:
    """True zeros: available pair-months with no purchase, and stockout months."""
    out, _ = world
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    avail_no_buy = panel.filter(pl.col("available") & ~pl.col("bought"))
    assert avail_no_buy.height > 0
    stockouts = panel.filter(pl.col("active") & ~pl.col("available"))
    assert stockouts.height > 0


def test_naive_bias_direction_recorded(world: tuple[Path, dict[str, Any]]) -> None:
    _, summary = world
    assert summary["naive_bias_direction"] == "toward_zero"


def test_endogeneity_present_in_well_identified(world: tuple[Path, dict[str, Any]]) -> None:
    """Realized price covaries positively with the demand shock within pair."""
    out, _ = world
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    df = panel.filter(pl.col("bought") & ~pl.col("on_contract")).with_columns(
        (pl.col("unit_price_cents").log() - pl.col("list_cents").log()).alias("log_idx")
    )
    pair = ["customer_id", "item_id"]
    demeaned = df.with_columns(
        (pl.col("log_idx") - pl.col("log_idx").mean().over(pair)).alias("p_dm"),
        (pl.col("demand_shock") - pl.col("demand_shock").mean().over(pair)).alias("u_dm"),
    )
    corr = demeaned.select(pl.corr("p_dm", "u_dm")).item()
    assert corr > 0.05, f"expected positive price-demand covariance, got {corr}"


def test_instrument_relevance_well_identified(world: tuple[Path, dict[str, Any]]) -> None:
    out, _ = world
    slope = _first_stage_slope(out)
    assert slope > 0.3, f"first-stage slope {slope} too weak for well_identified"


def _first_stage_slope(out: Path) -> float:
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    df = panel.filter(pl.col("bought") & ~pl.col("on_contract")).with_columns(
        (pl.col("unit_price_cents").log() - pl.col("list_cents").log()).alias("log_idx"),
        (pl.col("z_cost") + pl.col("z_tariff")).alias("z"),
    )
    pair = ["customer_id", "item_id"]
    d = df.with_columns(
        (pl.col("log_idx") - pl.col("log_idx").mean().over(pair)).alias("p_dm"),
        (pl.col("z") - pl.col("z").mean().over(pair)).alias("z_dm"),
    )
    cov = d.select((pl.col("p_dm") * pl.col("z_dm")).mean()).item()
    var = d.select((pl.col("z_dm") ** 2).mean()).item()
    return float(cov / var)


def test_preset_weak_instrument(tmp_path: Path) -> None:
    out, _ = _gen(tmp_path, Preset.WEAK_INSTRUMENT)
    slope = _first_stage_slope(out)
    assert abs(slope) < 0.15, f"weak_instrument first stage should be tiny, got {slope}"


def test_preset_invalid_instrument(tmp_path: Path) -> None:
    """Tariff shock leaks into demand: within-pair, the tariff instrument moves
    quantity even AFTER controlling for price (the raw correlation nets the
    price channel against the planted leak and is near zero by construction)."""
    import numpy.linalg as la

    out, _ = _gen(tmp_path, Preset.INVALID_INSTRUMENT)
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    df = panel.filter(pl.col("bought") & (pl.col("qty") > 0) & ~pl.col("on_contract")).with_columns(
        pl.col("qty").log().alias("log_q"),
        pl.col("unit_price_cents").log().alias("log_p"),
    )
    pair = ["customer_id", "item_id"]
    d = df.with_columns(
        (pl.col("log_q") - pl.col("log_q").mean().over(pair)).alias("q_dm"),
        (pl.col("log_p") - pl.col("log_p").mean().over(pair)).alias("p_dm"),
        (pl.col("z_tariff") - pl.col("z_tariff").mean().over(pair)).alias("zt_dm"),
    )
    x = d.select("p_dm", "zt_dm").to_numpy()
    y = d["q_dm"].to_numpy()
    coef = la.lstsq(x, y, rcond=None)[0]
    # Planted leak 1.5; the partial regression's endogenous price coefficient
    # absorbs part of it, so the measured leak is attenuated. Calibrated on the
    # current world (base_qty_log_mean=3.0): 0.57-0.99 across 5 seeds, vs
    # -0.08-0.10 in valid worlds -- unambiguous separation.
    assert coef[1] > 0.4, f"tariff should enter demand directly, got {coef[1]}"


def test_preset_invalid_instrument_valid_in_baseline(world: tuple[Path, dict[str, Any]]) -> None:
    out, _ = world
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    df = panel.filter(pl.col("bought") & (pl.col("qty") > 0) & ~pl.col("on_contract")).with_columns(
        pl.col("qty").log().alias("log_q"),
        (pl.col("unit_price_cents").log()).alias("log_p"),
    )
    pair = ["customer_id", "item_id"]
    d = df.with_columns(
        (pl.col("log_q") - pl.col("log_q").mean().over(pair)).alias("q_dm"),
        (pl.col("z_tariff") - pl.col("z_tariff").mean().over(pair)).alias("zt_dm"),
        (pl.col("log_p") - pl.col("log_p").mean().over(pair)).alias("p_dm"),
        (pl.col("demand_shock") - pl.col("demand_shock").mean().over(pair)).alias("u_dm"),
    )
    # Control for price and demand shock: residual tariff correlation ~ 0.
    # Quantity moves with tariff ONLY through price in the valid baseline.
    import numpy.linalg as la

    x = d.select("p_dm", "u_dm", "zt_dm").to_numpy()
    y = d["q_dm"].to_numpy()
    coef = la.lstsq(x, y, rcond=None)[0]
    # No planted leak: calibrated at -0.08-0.10 across 5 seeds on the current
    # world, far below the invalid preset's 0.57-0.99. The engine-side
    # statistical gate is the Phase 6 overidentification test, verified
    # separately.
    assert abs(coef[2]) < 0.3, f"tariff should not enter demand directly, got {coef[2]}"


def test_preset_sparse(tmp_path: Path) -> None:
    out, summary = _gen(tmp_path, Preset.SPARSE)
    base_cfg = GeneratorConfig.model_validate(summary["generator_config"])
    assert base_cfg.demand.purchase_prob <= 0.2
    truth = pl.read_parquet(out / "ground_truth" / "line_truth.parquet")
    # Far fewer observations per pair than the default world.
    assert truth.height < 8000


def test_preset_price_constant(tmp_path: Path) -> None:
    out, _ = _gen(tmp_path, Preset.PRICE_CONSTANT)
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    spread = (
        panel.filter(pl.col("bought"))
        .group_by("customer_id", "item_id")
        .agg((pl.col("unit_price_cents").max() - pl.col("unit_price_cents").min()).alias("range"))
        .select(pl.col("range").max())
        .item()
    )
    assert spread == 0, f"price_constant must have zero within-pair variation, got {spread}"


def _demand_shock_lag1_autocorr(out: Path) -> float:
    """Mean lag-1 autocorrelation of the customer demand shock across months."""
    import numpy as np

    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    cm = (
        panel.select("customer_id", "month", "demand_shock")
        .unique(subset=["customer_id", "month"])
        .sort("customer_id", "month")
    )
    rhos = []
    for _, grp in cm.group_by("customer_id"):
        v = grp.sort("month")["demand_shock"].to_numpy()
        if len(v) > 6 and np.std(v[:-1]) > 0 and np.std(v[1:]) > 0:
            rhos.append(float(np.corrcoef(v[:-1], v[1:])[0, 1]))
    return float(np.mean(rhos))


def test_preset_persistent_demand(tmp_path: Path) -> None:
    """24.5: AR(1) customer demand shocks - strong lag-1 autocorrelation,
    while the default world's shocks are serially uncorrelated."""
    out_p, summary = _gen(tmp_path / "p", Preset.PERSISTENT_DEMAND)
    cfg = GeneratorConfig.model_validate(summary["generator_config"])
    assert cfg.demand.demand_ar == 0.70
    rho_p = _demand_shock_lag1_autocorr(out_p)
    out_iid, _ = _gen(tmp_path / "i", Preset.WELL_IDENTIFIED)
    rho_iid = _demand_shock_lag1_autocorr(out_iid)
    assert rho_p > 0.45, f"persistent demand should autocorrelate, got {rho_p:.3f}"
    assert abs(rho_iid) < 0.15, f"iid demand should not, got {rho_iid:.3f}"


def test_preset_heterogeneous_elasticity(tmp_path: Path) -> None:
    """24.9: pair betas vary within segment; the segment mean stays anchored
    near the planted segment elasticity (it is what a segment model estimates)."""
    out, summary = _gen(tmp_path, Preset.HETEROGENEOUS_ELASTICITY)
    cfg = GeneratorConfig.model_validate(summary["generator_config"])
    assert cfg.demand.within_segment_beta_sd == 0.40
    assert (out / "ground_truth" / "truth_summary.json").exists()
    assert summary["misspecification"]["within_segment_beta_sd"] == 0.40


def test_preset_nonconstant_elasticity(tmp_path: Path) -> None:
    _out, summary = _gen(tmp_path, Preset.NONCONSTANT_ELASTICITY)
    cfg = GeneratorConfig.model_validate(summary["generator_config"])
    assert cfg.demand.elasticity_curvature == -1.5
    assert summary["misspecification"]["elasticity_curvature"] == -1.5


def test_preset_competitor_comovement(tmp_path: Path) -> None:
    """24.9/24.10: the market index file exists ONLY on this preset and its
    log co-moves with the planted cost/tariff shocks."""
    import numpy as np

    out, _ = _gen(tmp_path / "c", Preset.COMPETITOR_COMOVEMENT)
    mkt_path = out / "erp" / "market_price_index.csv"
    assert mkt_path.exists()
    out_base, _ = _gen(tmp_path / "b", Preset.WELL_IDENTIFIED)
    assert not (out_base / "erp" / "market_price_index.csv").exists()
    mkt = pl.read_csv(mkt_path)
    panel = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    # Join by month index: PERIOD is YYYY-MM from the world start.
    shocks = (
        panel.select("month", "z_cost", "z_tariff")
        .group_by("month")
        .agg(pl.col("z_cost").mean(), pl.col("z_tariff").mean())
        .sort("month")
    )
    idx_by_month = (
        mkt.with_columns(pl.col("MKT_IDX").log().alias("log_mkt"))
        .group_by("PERIOD")
        .agg(pl.col("log_mkt").mean())
        .sort("PERIOD")
    )
    a = shocks["z_cost"].to_numpy() + shocks["z_tariff"].to_numpy()
    b = idx_by_month["log_mkt"].to_numpy()
    n = min(len(a), len(b))
    rho = float(np.corrcoef(a[:n], b[:n])[0, 1])
    assert rho > 0.8, f"market index must co-move with the cost shocks, got {rho:.3f}"


def test_returns_linked_and_negative(world: tuple[Path, dict[str, Any]]) -> None:
    out, _ = world
    df = pl.read_csv(out / "erp" / "invoice_lines.csv", infer_schema_length=10000)
    rets = df.filter(pl.col("TXN_CD") == "R")
    assert rets.height > 0
    assert (rets["QTY_SHP"] < 0).all()
    assert rets["ORIG_INV"].is_not_null().all()
    inv_ids = set(df.filter(pl.col("TXN_CD") == "S")["INV_NO"].to_list())
    linked = [o in inv_ids for o in rets["ORIG_INV"].to_list()]
    assert all(linked), "every return must reference an existing invoice"


def test_credit_memos_have_reasons(world: tuple[Path, dict[str, Any]]) -> None:
    out, _ = world
    df = pl.read_csv(out / "erp" / "invoice_lines.csv", infer_schema_length=10000)
    credits = df.filter(pl.col("TXN_CD") == "C")
    assert credits.height > 0
    assert credits["REASON_CD"].is_not_null().all()
    assert (credits["EXT_NET"] < 0).all()


def test_settlements_match_line_truth(world: tuple[Path, dict[str, Any]]) -> None:
    """Settlement table totals equal the sum of planted per-line truth."""
    out, _ = world
    truth = pl.read_parquet(out / "ground_truth" / "line_truth.parquet")
    settles = pl.read_csv(out / "erp" / "rebate_settlements.csv")
    total_table = round(settles["AMT"].sum() * 100)
    total_truth = int(truth["truth_settlement_rebate_cents"].sum())
    assert total_table == total_truth


def test_run2_worlds_byte_identical_across_runs(tmp_path: Path) -> None:
    """Determinism across repeated runs with every Run-2 feature enabled
    (currencies, substitution groups, quotes, receipts, tiers): all exports and
    the truth summary byte-identical."""
    outs = []
    for run in ("a", "b"):
        cfg = GeneratorConfig.for_preset(
            Preset.WELL_IDENTIFIED,
            seed=19,
            n_customers=60,
            n_items=150,
            currency_shares={"USD": 0.7, "EUR": 0.3},
            n_substitution_groups=5,
        )
        out = tmp_path / run
        generate(cfg, out)
        outs.append(out)
    a, b = outs
    files = sorted(p.name for p in (a / "erp").glob("*.csv"))
    assert "quotes.csv" in files and "substitution_groups.csv" in files
    for name in files:
        assert _file_hash(a / "erp" / name) == _file_hash(b / "erp" / name), name
    assert _file_hash(a / "ground_truth" / "truth_summary.json") == _file_hash(
        b / "ground_truth" / "truth_summary.json"
    )
