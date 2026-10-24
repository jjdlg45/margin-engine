"""Validation without planted truth (Run 4, 27.4).

Real data carries no ground truth, so these checks ask what CAN be verified:

- Temporal backtest: fit elasticity on months 1..T-k only, then measure how
  well price changes predict volume changes in the held-out months, against a
  naive no-price-effect baseline. Both errors are reported; nothing is gated.
- Placebo tests: (a) instruments shuffled across the units they vary over
  (suppliers / categories) must show no effect - the placebo estimate
  distribution should straddle zero and lose its first stage; (b) fake
  price-change dates per pair must show no volume response.
- Stability: per-segment elasticity re-estimated on rolling windows, and the
  commercial segmentation refit per window with partition agreement (ARI)
  against the full-sample segments.

Everything here is informational and reported in the "Real-data validation"
report section; large segments run on a seeded pair subsample, labeled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from margin_engine.config import Rules
from margin_engine.fiscal import FiscalCalendar
from margin_engine.sensitivity.estimate import (
    STATUS_CAUSAL,
    STATUS_IV,
    _cluster_ols,
    _first_stage_f,
    _make_cluster_spec,
    _to_pandas,
    _two_way_demean,
    estimate_segment,
)


@dataclass
class RealValidationResult:
    backtest: pl.DataFrame
    placebo: pl.DataFrame
    stability_elasticity: pl.DataFrame
    stability_segments: pl.DataFrame
    notes: list[str] = field(default_factory=list)


def _segments(panel: pl.DataFrame) -> list[int]:
    return sorted(int(s) for s in panel["segment_leaf"].unique().to_list() if s is not None)


def _subsample_pairs(
    work: pl.DataFrame, max_rows: int, seed: int, notes: list[str], where: str
) -> pl.DataFrame:
    """Deterministic pair-level subsample when a segment is very large. The
    diagnostic stays valid (pairs are the sampling unit); the label says so."""
    if work.height <= max_rows:
        return work
    pairs = work["pair"].unique().sort()
    rng = np.random.default_rng(seed)
    frac = max_rows / work.height
    keep = pairs.filter(pl.Series(rng.random(len(pairs)) < frac))
    out = work.filter(pl.col("pair").is_in(keep.to_list()))
    notes.append(
        f"{where}: segment exceeded {max_rows:,} rows; computed on a seeded "
        f"pair subsample ({out.height:,} of {work.height:,} rows)"
    )
    return out


# ---------------------------------------------------------------------------
# Temporal backtest
# ---------------------------------------------------------------------------


def backtest_elasticity(panel: pl.DataFrame, rules: Rules) -> tuple[pl.DataFrame, list[str]]:
    """Per segment: fit on months 1..T-k, then compare held-out volume-change
    prediction error with the fitted elasticity vs a zero-elasticity baseline.

    Changes are within-pair (test month vs the pair's last positive train
    month) and demeaned per test month, so common demand shifts cancel and the
    comparison isolates the price term.
    """
    sc = rules.sensitivity
    notes: list[str] = []
    k = sc.backtest_holdout_months
    months = sorted(panel["month_idx"].unique().to_list())
    rows: list[dict[str, Any]] = []
    if len(months) < k + 12:
        notes.append(
            f"temporal backtest skipped: {len(months)} months of data, needs at "
            f"least {k + 12} (holdout {k} + 12 training months)"
        )
        return pl.DataFrame(rows), notes
    cutoff = months[-k - 1]

    for seg in _segments(panel):
        seg_panel = panel.filter(pl.col("segment_leaf") == seg)
        train = seg_panel.filter(pl.col("month_idx") <= cutoff)
        est = estimate_segment(train, rules, seg)
        if est.iv is not None and est.status in (STATUS_IV, STATUS_CAUSAL):
            beta, model = est.iv, "iv"
        elif est.naive_fe_ols is not None:
            beta, model = est.naive_fe_ols, "fe_ols"
        else:
            rows.append(
                {
                    "segment": seg,
                    "model": None,
                    "beta_train": None,
                    "n_test_changes": 0,
                    "rmse_model": None,
                    "rmse_naive": None,
                    "improvement": None,
                    "beta_out_of_sample": None,
                    "status": "insufficient_data",
                }
            )
            continue

        pos = seg_panel.filter(
            (pl.col("qty") > 0)
            & pl.col("contract_id").is_null()
            & pl.col("log_qty").is_not_null()
            & pl.col("log_price").is_not_null()
        )
        base = (
            pos.filter(pl.col("month_idx") <= cutoff)
            .sort("month_idx")
            .group_by("pair", maintain_order=True)
            .agg(
                pl.col("log_qty").last().alias("lq0"),
                pl.col("log_price").last().alias("lp0"),
            )
        )
        test = pos.filter(pl.col("month_idx") > cutoff).join(base, on="pair", how="inner")
        if test.height < 30:
            rows.append(
                {
                    "segment": seg,
                    "model": model,
                    "beta_train": beta,
                    "n_test_changes": test.height,
                    "rmse_model": None,
                    "rmse_naive": None,
                    "improvement": None,
                    "beta_out_of_sample": None,
                    "status": "insufficient_data",
                }
            )
            continue
        test = test.with_columns(
            (pl.col("log_qty") - pl.col("lq0")).alias("dlq"),
            (pl.col("log_price") - pl.col("lp0")).alias("dlp"),
        ).with_columns(
            (pl.col("dlq") - pl.col("dlq").mean().over("month_idx")).alias("dlq_dm"),
            (pl.col("dlp") - pl.col("dlp").mean().over("month_idx")).alias("dlp_dm"),
        )
        dlq = test["dlq_dm"].to_numpy()
        dlp = test["dlp_dm"].to_numpy()
        rmse_model = float(np.sqrt(np.mean((dlq - beta * dlp) ** 2)))
        rmse_naive = float(np.sqrt(np.mean(dlq**2)))
        denom = float(dlp @ dlp)
        beta_oos = float((dlp @ dlq) / denom) if denom > 1e-12 else None
        rows.append(
            {
                "segment": seg,
                "model": model,
                "beta_train": beta,
                "n_test_changes": test.height,
                "rmse_model": rmse_model,
                "rmse_naive": rmse_naive,
                "improvement": 1 - rmse_model / rmse_naive if rmse_naive > 0 else None,
                "beta_out_of_sample": beta_oos,
                "status": "ok",
            }
        )
    notes.append(
        f"temporal backtest: trained through month index {cutoff}, held out the "
        f"last {k} months; errors are within-pair log-volume changes demeaned "
        "per test month (informational, not gated)"
    )
    return pl.DataFrame(rows), notes


# ---------------------------------------------------------------------------
# Placebo tests
# ---------------------------------------------------------------------------


def _derangement_idx(rng: np.random.Generator, n: int) -> np.ndarray:
    """Index permutation with no fixed points."""
    if n < 2:
        return np.arange(n)
    while True:
        p = rng.permutation(n)
        if not np.any(p == np.arange(n)):
            return p


def _permute_in_time(
    series: pl.DataFrame, unit_col: str, z_col: str, rng: np.random.Generator
) -> pl.DataFrame:
    """Shuffle each unit's instrument series across periods (derangement).

    Shuffling ACROSS units would stay contaminated: with few suppliers or
    categories, month demeaning mixes every unit's series into every row's
    quantity, so another unit's real series still correlates with demeaned
    volumes by construction. Permuting in time within the unit destroys the
    alignment with every unit's contemporaneous values instead."""
    parts = []
    for grp in series.sort(unit_col, "period").partition_by(unit_col, maintain_order=True):
        z = grp[z_col].to_numpy()
        parts.append(grp.with_columns(pl.Series(z_col, z[_derangement_idx(rng, len(z))])))
    return pl.concat(parts)


def _reduced_form_and_f(
    pdf: Any, z_cols: list[str], spec: Any, time_fe: str
) -> tuple[float, float, float | None]:
    """(reduced-form Wald p-value of log_qty on the instruments, first-stage F,
    2SLS beta). With an irrelevant instrument the 2SLS ratio concentrates
    around OLS, NOT zero - so the placebo verdict rests on the reduced form
    and the first stage, never on the ratio."""
    from scipy import stats

    dm = _two_way_demean(pdf, ["log_qty", "log_price", *z_cols], "pair", time_fe)
    y = dm["log_qty"].to_numpy()
    p = dm["log_price"].to_numpy()
    z = dm[z_cols].to_numpy()
    beta_rf, vcov_rf = _cluster_ols(y, z, spec)
    try:
        wald = float(beta_rf @ np.linalg.pinv(vcov_rf) @ beta_rf)
    except np.linalg.LinAlgError:
        wald = 0.0
    p_rf = float(1 - stats.chi2.cdf(wald, z.shape[1]))
    f = _first_stage_f(p, z, spec)
    zz_inv = np.linalg.pinv(z.T @ z)
    p_hat = z @ (zz_inv @ (z.T @ p))
    denom = float(p_hat @ p)
    beta_iv = float((p_hat @ y) / denom) if abs(denom) > 1e-12 else None
    return p_rf, f, beta_iv


def placebo_tests(
    panel: pl.DataFrame, rules: Rules, seed: int = 0
) -> tuple[pl.DataFrame, list[str]]:
    """Per segment: (a) shuffled-instrument IV estimates, where instruments are
    permuted across the units they vary over (supplier identity for z_cost,
    category identity for z_tariff) so any apparent effect is spurious by
    construction; (b) fake price-change-date event coefficients. Both
    distributions should straddle zero."""
    sc = rules.sensitivity
    notes: list[str] = []
    n_draws = sc.placebo_draws
    rows: list[dict[str, Any]] = []

    for seg in _segments(panel):
        work = panel.filter(
            (pl.col("segment_leaf") == seg)
            & pl.col("contract_id").is_null()
            & (pl.col("qty") > 0)
            & pl.col("log_qty").is_not_null()
            & pl.col("log_price").is_not_null()
        )
        have_z = (
            "z_cost" in work.columns
            and "z_tariff" in work.columns
            and work["z_cost"].null_count() == 0
            and work["z_tariff"].null_count() == 0
            and "supplier_id" in work.columns
            and "item_category" in work.columns
        )
        if work.height < sc.min_observations:
            continue
        work = _subsample_pairs(
            work, sc.real_validation_max_rows, seed + seg, notes, f"placebo (segment {seg})"
        )
        time_fe = "role_month" if "role_month" in work.columns else "month_idx"
        keep = [
            "log_qty",
            "log_price",
            "pair",
            "month_idx",
            "cluster_month",
            *([time_fe] if time_fe != "month_idx" else []),
            *(["z_cost", "z_tariff", "supplier_id", "item_category", "period"] if have_z else []),
        ]
        pdf = _to_pandas(work.select(keep))
        spec = _make_cluster_spec(pdf["cluster_month"].to_numpy())
        rng = np.random.default_rng(seed + 1000 + seg)

        # (a) shuffled instruments --------------------------------------------
        if have_z:
            actual_p_rf, actual_f, actual_beta = _reduced_form_and_f(
                pdf, ["z_cost", "z_tariff"], spec, time_fe
            )
            sup_series = work.select("supplier_id", "period", "z_cost").unique()
            cat_series = work.select("item_category", "period", "z_tariff").unique()
            rf_pvals: list[float] = []
            fstats: list[float] = []
            base_cols = work.select(
                "log_qty",
                "log_price",
                "pair",
                "month_idx",
                "cluster_month",
                *([time_fe] if time_fe != "month_idx" else []),
                "supplier_id",
                "item_category",
                "period",
            )
            for _d in range(n_draws):
                sup_p = _permute_in_time(sup_series, "supplier_id", "z_cost", rng)
                cat_p = _permute_in_time(cat_series, "item_category", "z_tariff", rng)
                shuffled = (
                    base_cols.join(
                        sup_p.rename({"z_cost": "z_cost_p"}),
                        on=["supplier_id", "period"],
                        how="left",
                    )
                    .join(
                        cat_p.rename({"z_tariff": "z_tariff_p"}),
                        on=["item_category", "period"],
                        how="left",
                    )
                    .with_columns(
                        pl.col("z_cost_p").fill_null(0.0), pl.col("z_tariff_p").fill_null(0.0)
                    )
                )
                spdf = _to_pandas(
                    shuffled.select(
                        "log_qty",
                        "log_price",
                        "z_cost_p",
                        "z_tariff_p",
                        "pair",
                        "month_idx",
                        "cluster_month",
                        *([time_fe] if time_fe != "month_idx" else []),
                    )
                )
                # The join may reorder rows; the cluster spec must be built
                # from the same frame the estimate runs on.
                sspec = _make_cluster_spec(spdf["cluster_month"].to_numpy())
                p_rf, f, _b = _reduced_form_and_f(spdf, ["z_cost_p", "z_tariff_p"], sspec, time_fe)
                rf_pvals.append(p_rf)
                fstats.append(f)
            if rf_pvals:
                # The placebo statistic is the REDUCED FORM (instrument ->
                # quantity): with a shuffled instrument its p-values should be
                # uniform (share significant near 5%) and the first stage
                # should die. The 2SLS ratio is NOT a valid placebo statistic:
                # with an irrelevant instrument it concentrates around OLS.
                arr = np.array(rf_pvals)
                rows.append(
                    {
                        "segment": seg,
                        "test": "shuffled_instrument",
                        "n_draws": len(rf_pvals),
                        "actual": actual_p_rf,
                        "actual_first_stage_f": actual_f,
                        "actual_iv_beta": actual_beta,
                        "placebo_mean": float(arr.mean()),
                        "placebo_sd": float(arr.std(ddof=1)) if len(arr) > 1 else None,
                        "placebo_p025": float(np.quantile(arr, 0.025)),
                        "placebo_p975": float(np.quantile(arr, 0.975)),
                        "share_strong_first_stage": float(
                            np.mean(np.array(fstats) >= sc.min_first_stage_f)
                        ),
                        "share_significant": float(np.mean(arr < 0.05)),
                    }
                )

        # (b) fake price-change dates ------------------------------------------
        pair_span = work.group_by("pair").agg(
            pl.col("month_idx").min().alias("m0"), pl.col("month_idx").max().alias("m1")
        )
        eligible = pair_span.filter(pl.col("m1") > pl.col("m0")).sort("pair")
        if eligible.height >= 20:
            coefs: list[float] = []
            tstats: list[float] = []
            m0 = eligible["m0"].to_numpy()
            m1 = eligible["m1"].to_numpy()
            for _d in range(n_draws):
                fake_m = m0 + (rng.random(len(m0)) * (m1 - m0 + 1)).astype(np.int64)
                fake = eligible.select("pair").with_columns(pl.Series("fake_m", fake_m))
                w = work.join(fake, on="pair", how="inner").with_columns(
                    (pl.col("month_idx") >= pl.col("fake_m")).cast(pl.Float64).alias("post")
                )
                wpdf = _to_pandas(
                    w.select(
                        "log_qty",
                        "post",
                        "pair",
                        "month_idx",
                        "cluster_month",
                        *([time_fe] if time_fe != "month_idx" else []),
                    )
                )
                wspec = _make_cluster_spec(wpdf["cluster_month"].to_numpy())
                dm = _two_way_demean(wpdf, ["log_qty", "post"], "pair", time_fe)
                x = dm[["post"]].to_numpy()
                if float((x**2).sum()) < 1e-12:
                    continue
                b_arr, v = _cluster_ols(dm["log_qty"].to_numpy(), x, wspec)
                se = float(np.sqrt(v[0, 0])) if v[0, 0] > 0 else None
                coefs.append(float(b_arr[0]))
                if se:
                    tstats.append(float(b_arr[0]) / se)
            if coefs:
                carr = np.array(coefs)
                rows.append(
                    {
                        "segment": seg,
                        "test": "fake_price_change_dates",
                        "n_draws": len(coefs),
                        "placebo_mean": float(carr.mean()),
                        "placebo_sd": float(carr.std(ddof=1)) if len(carr) > 1 else None,
                        "placebo_p025": float(np.quantile(carr, 0.025)),
                        "placebo_p975": float(np.quantile(carr, 0.975)),
                        "share_significant": float(np.mean(np.abs(np.array(tstats)) > 1.96))
                        if tstats
                        else None,
                    }
                )
    notes.append(
        f"placebo tests: {n_draws} draws per segment per test; shuffled "
        "instruments should lose their reduced-form effect and first stage "
        "(the 2SLS ratio is not a valid placebo statistic: with an irrelevant "
        "instrument it concentrates around OLS), fake price-change dates "
        "should show no volume response (informational)"
    )
    schema = {
        "segment": pl.Int64,
        "test": pl.String,
        "n_draws": pl.Int64,
        # shuffled_instrument: actual = reduced-form Wald p-value of quantity
        # on the REAL instruments; placebo_* summarize the p-value distribution
        # across shuffled draws (uniform under the null -> mean near 0.5).
        # fake_price_change_dates: placebo_* summarize the event coefficient.
        "actual": pl.Float64,
        "actual_first_stage_f": pl.Float64,
        "actual_iv_beta": pl.Float64,
        "placebo_mean": pl.Float64,
        "placebo_sd": pl.Float64,
        "placebo_p025": pl.Float64,
        "placebo_p975": pl.Float64,
        "share_strong_first_stage": pl.Float64,
        "share_significant": pl.Float64,
    }
    if not rows:
        return pl.DataFrame(schema=schema), notes
    return pl.DataFrame([{**dict.fromkeys(schema), **r} for r in rows], schema=schema), notes


# ---------------------------------------------------------------------------
# Rolling-window stability
# ---------------------------------------------------------------------------


def rolling_stability(
    panel: pl.DataFrame,
    seg_frame: pl.DataFrame,
    rules: Rules,
    fiscal: FiscalCalendar | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, list[str]]:
    """Elasticity (naive FE-OLS, cheap and always defined) per segment per
    rolling window, plus the commercial segmentation refit per window with ARI
    against the full-sample partition on the window's lines."""
    sc = rules.sensitivity
    notes: list[str] = []
    window = sc.stability_window_months
    step = sc.stability_step_months
    months = sorted(panel["month_idx"].unique().to_list())
    el_rows: list[dict[str, Any]] = []
    seg_rows: list[dict[str, Any]] = []
    if len(months) < window + step:
        notes.append(
            f"rolling stability skipped: {len(months)} months of data, needs at "
            f"least {window + step} (window {window} + step {step})"
        )
        return pl.DataFrame(el_rows), pl.DataFrame(seg_rows), notes

    starts = list(range(0, len(months) - window + 1, step))
    from sklearn.metrics import adjusted_rand_score

    from margin_engine.segment.trees import fit_commercial_segments

    for w_i, s in enumerate(starts):
        lo, hi = months[s], months[s + window - 1]
        label = f"m{lo}-m{hi}"
        win = panel.filter(
            (pl.col("month_idx") >= lo)
            & (pl.col("month_idx") <= hi)
            & (pl.col("qty") > 0)
            & pl.col("contract_id").is_null()
            & pl.col("log_qty").is_not_null()
            & pl.col("log_price").is_not_null()
        )
        for seg in _segments(panel):
            wseg = win.filter(pl.col("segment_leaf") == seg)
            if wseg.height < sc.min_observations:
                continue
            time_fe = "role_month" if "role_month" in wseg.columns else "month_idx"
            pdf = _to_pandas(
                wseg.select(
                    "log_qty",
                    "log_price",
                    "pair",
                    "month_idx",
                    "cluster_month",
                    *([time_fe] if time_fe != "month_idx" else []),
                )
            )
            spec = _make_cluster_spec(pdf["cluster_month"].to_numpy())
            dm = _two_way_demean(pdf, ["log_qty", "log_price"], "pair", time_fe)
            x = dm[["log_price"]].to_numpy()
            if float((x**2).sum()) < 1e-12:
                continue
            beta, vcov = _cluster_ols(dm["log_qty"].to_numpy(), x, spec)
            el_rows.append(
                {
                    "window": label,
                    "window_index": w_i,
                    "segment": seg,
                    "n_obs": wseg.height,
                    "fe_ols": float(beta[0]),
                    "se": float(np.sqrt(vcov[0, 0])) if vcov[0, 0] > 0 else None,
                }
            )

        # Segmentation stability: refit on the window's lines, compare with ARI.
        if "invoice_date" in seg_frame.columns and "segment_leaf" in seg_frame.columns:
            # The window bounds are panel period indexes: map the lines with
            # the same fiscal calendar (Phase 36; default = year*12+month).
            if fiscal is None or fiscal.is_calendar_months:
                win_lines = seg_frame.filter(
                    (
                        pl.col("invoice_date").dt.year() * 12 + pl.col("invoice_date").dt.month()
                    ).is_between(lo, hi)
                )
            else:
                win_lines = (
                    fiscal.attach(seg_frame, "invoice_date", idx="_fp_idx")
                    .filter(pl.col("_fp_idx").is_between(lo, hi))
                    .drop("_fp_idx")
                )
            try:
                win_fit = fit_commercial_segments(win_lines, rules, seed=0)
                joined = (
                    win_fit.leaf_assignments.rename({"segment_leaf": "win_leaf"})
                    .join(
                        seg_frame.select("source_line_key", "segment_leaf"),
                        on="source_line_key",
                        how="inner",
                    )
                    .drop_nulls()
                )
                if joined.height:
                    ari = float(
                        adjusted_rand_score(
                            joined["segment_leaf"].to_numpy(), joined["win_leaf"].to_numpy()
                        )
                    )
                    seg_rows.append(
                        {
                            "window": label,
                            "window_index": w_i,
                            "n_lines": joined.height,
                            "n_segments_window": win_fit.leaf_assignments[
                                "segment_leaf"
                            ].n_unique(),
                            "ari_vs_full_sample": ari,
                        }
                    )
            except Exception as exc:  # pragma: no cover - thin windows
                notes.append(f"segment stability window {label}: refit failed ({exc})")
    notes.append(
        f"rolling stability: {len(starts)} windows of {window} months, step "
        f"{step}; elasticity is the within-pair FE-OLS per window "
        "(comparable across windows, not the headline IV), segmentation ARI "
        "compares each window's refit partition with the full-sample segments"
    )
    return pl.DataFrame(el_rows), pl.DataFrame(seg_rows), notes


def run_real_validation(
    panel: pl.DataFrame,
    seg_frame: pl.DataFrame,
    rules: Rules,
    seed: int = 0,
    fiscal: FiscalCalendar | None = None,
) -> RealValidationResult:
    notes: list[str] = []
    bt, n1 = backtest_elasticity(panel, rules)
    pb, n2 = placebo_tests(panel, rules, seed)
    st_el, st_seg, n3 = rolling_stability(panel, seg_frame, rules, fiscal)
    notes.extend(n1 + n2 + n3)
    return RealValidationResult(
        backtest=bt,
        placebo=pb,
        stability_elasticity=st_el,
        stability_segments=st_seg,
        notes=notes,
    )
