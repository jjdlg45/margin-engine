"""Run 4 (27.4): validation without planted truth. A controlled panel DGP with
known elasticity shows the backtest beats the no-price baseline, shuffled
instruments lose their effect and first stage, fake price-change dates show
nothing, and rolling windows recover a stable elasticity."""

from __future__ import annotations

import numpy as np
import polars as pl

from margin_engine.config import Rules
from margin_engine.sensitivity.real_validation import (
    backtest_elasticity,
    placebo_tests,
    rolling_stability,
    run_real_validation,
)

BETA = -2.0


def _panel(n_pairs: int = 80, months: int = 24, seed: int = 3) -> pl.DataFrame:
    """Pair and month effects, instruments driving price, log-linear demand."""
    rng = np.random.default_rng(seed)
    n_sup, n_cat = 6, 4
    sup_of = rng.integers(0, n_sup, n_pairs)
    cat_of = rng.integers(0, n_cat, n_pairs)
    pair_fe = rng.normal(3.0, 0.4, n_pairs)
    rows = []
    z_cost = rng.normal(0, 0.08, (n_sup, months))
    z_tariff = rng.normal(0, 0.05, (n_cat, months))
    month_fe = rng.normal(0, 0.15, months)
    for i in range(n_pairs):
        base_lp = rng.normal(2.0, 0.3)
        for t in range(months):
            lp = (
                base_lp
                + 0.9 * z_cost[sup_of[i], t]
                + 0.8 * z_tariff[cat_of[i], t]
                + rng.normal(0, 0.02)
            )
            lq = pair_fe[i] + month_fe[t] + BETA * lp + rng.normal(0, 0.10)
            period = f"{2024 + t // 12}-{t % 12 + 1:02d}"
            rows.append(
                {
                    "pair": f"C{i:03d}|I{i:03d}",
                    "customer_id": f"C{i:03d}",
                    "item_id": f"I{i:03d}",
                    "month_idx": 24288 + t,  # arbitrary consistent month index
                    "period": period,
                    "qty": float(np.exp(lq)),
                    "log_qty": lq,
                    "log_price": lp,
                    "z_cost": float(z_cost[sup_of[i], t]),
                    "z_tariff": float(z_tariff[cat_of[i], t]),
                    "supplier_id": f"S{sup_of[i]}",
                    "item_category": f"CAT{cat_of[i]}",
                    "cluster_month": period,
                    "cluster_customer": f"C{i:03d}",
                    "cluster_supplier_month": f"S{sup_of[i]}@{period}",
                    "contract_id": None,
                    "price_carried_forward": False,
                    "segment_leaf": 1,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("contract_id").cast(pl.String))


def test_backtest_beats_naive_baseline() -> None:
    bt, notes = backtest_elasticity(_panel(), Rules())
    assert bt.height == 1
    row = bt.to_dicts()[0]
    assert row["status"] == "ok"
    assert row["rmse_model"] < row["rmse_naive"]
    assert row["improvement"] > 0.2
    assert abs(row["beta_out_of_sample"] - BETA) < 0.5
    assert any("held out" in n for n in notes)


def test_backtest_skips_short_history() -> None:
    bt, notes = backtest_elasticity(_panel(months=10), Rules())
    assert bt.height == 0
    assert any("skipped" in n for n in notes)


def test_placebo_shuffled_instruments_show_no_effect() -> None:
    pb, _ = placebo_tests(_panel(), Rules(), seed=0)
    sh = pb.filter(pl.col("test") == "shuffled_instrument").to_dicts()
    assert len(sh) == 1
    row = sh[0]
    assert abs(row["actual_iv_beta"] - BETA) < 0.3  # the real estimate is real
    assert row["actual"] < 0.05  # real instruments: strong reduced form
    assert row["actual_first_stage_f"] > 10
    # Shuffled: the reduced form dies (p-values spread out, mostly not
    # significant) and the first stage dies. The 2SLS ratio is deliberately
    # NOT the statistic - with an irrelevant instrument it centers on OLS.
    assert row["placebo_mean"] > 0.1
    assert row["share_significant"] <= 0.3
    assert row["share_strong_first_stage"] <= 0.5


def test_placebo_fake_dates_show_no_effect() -> None:
    pb, _ = placebo_tests(_panel(), Rules(), seed=0)
    fk = pb.filter(pl.col("test") == "fake_price_change_dates").to_dicts()
    assert len(fk) == 1
    row = fk[0]
    assert abs(row["placebo_mean"]) < 0.1
    assert row["placebo_p025"] <= 0.0 <= row["placebo_p975"]


def test_rolling_elasticity_stable_on_stationary_dgp() -> None:
    st_el, st_seg, notes = rolling_stability(_panel(), pl.DataFrame(), Rules())
    assert st_el.height >= 2  # at least two 12-month windows out of 24 months
    for r in st_el.to_dicts():
        assert abs(r["fe_ols"] - BETA) < 0.5
    assert st_seg.height == 0  # no seg frame given -> no segment rows, no crash
    assert any("rolling stability" in n for n in notes)


def test_run_real_validation_bundles_everything() -> None:
    res = run_real_validation(_panel(), pl.DataFrame(), Rules(), seed=0)
    assert res.backtest.height == 1
    assert res.placebo.height == 2
    assert res.stability_elasticity.height >= 2
    assert len(res.notes) >= 3


def test_subsample_label_appears_when_segment_is_large() -> None:
    base = Rules()
    rules = base.model_copy(
        update={
            "sensitivity": base.sensitivity.model_copy(update={"real_validation_max_rows": 10_000})
        }
    )
    big = pl.concat([_panel(n_pairs=40)] * 12)  # 11,520 rows > 10,000 cap
    pb, notes = placebo_tests(big, rules, seed=0)
    assert any("pair subsample" in n for n in notes)
    assert pb.height >= 1
