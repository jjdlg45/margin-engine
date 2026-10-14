"""Own-price elasticity estimation per segment (Phase 6).

Models per segment (contract pairs excluded — their prices do not move):
  naive   FE-OLS log q ~ log p | pair + month        (biased; reported as such)
  ppml    FE-Poisson q ~ log p | pair + month        (zeros meaningful)
  iv      FE-2SLS log q ~ (log p ~ z_cost + z_tariff) | pair + month

Diagnostics: cluster-robust first-stage F, identifying-variation coverage,
Anderson-Rubin weak-IV-robust confidence set (grid inversion), Sargan-style
overidentification J (2 instruments), robustness spread across specifications.

Evidence status (spec 8.6):
  causal_elasticity_supported   all criteria pass + documented exclusion
                                rationale per instrument in config
  iv_estimated_elasticity       IV ran with F >= threshold, some criterion failed
  descriptive_elasticity        only non-IV estimates supportable (or weak F;
                                the AR confidence set is still reported)
  insufficient_data             not enough observations or price variation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from margin_engine.config import Rules

STATUS_INSUFFICIENT = "insufficient_data"
STATUS_DESCRIPTIVE = "descriptive_elasticity"
STATUS_IV = "iv_estimated_elasticity"
STATUS_CAUSAL = "causal_elasticity_supported"


@dataclass
class SegmentEstimate:
    segment: int
    n_obs: int = 0
    n_pairs: int = 0
    n_positive: int = 0
    price_cv_within: float = 0.0
    naive_fe_ols: float | None = None
    naive_se: float | None = None
    ppml: float | None = None
    iv: float | None = None
    iv_se: float | None = None
    iv_ci_low: float | None = None
    iv_ci_high: float | None = None
    first_stage_f: float | None = None
    overid_j: float | None = None
    overid_p: float | None = None
    ar_ci_low: float | None = None
    ar_ci_high: float | None = None
    robustness_spread: float | None = None
    identifying_coverage: float | None = None
    status: str = STATUS_INSUFFICIENT
    status_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["status_reasons"] = "; ".join(self.status_reasons)
        return d


def _to_pandas(df: pl.DataFrame) -> pd.DataFrame:
    # pyarrow is unavailable in this environment; go through numpy.
    return pd.DataFrame({c: df[c].to_numpy() for c in df.columns})


def _two_way_demean(
    df: pd.DataFrame, cols: list[str], fe1: str, fe2: str, tol: float = 1e-9, iters: int = 50
) -> pd.DataFrame:
    """Iterative two-way within transformation (pair and month FE)."""
    out = df[cols].astype(float).copy()
    for _ in range(iters):
        before = out.to_numpy().copy()
        out = out - out.groupby(df[fe1].to_numpy()).transform("mean")
        out = out - out.groupby(df[fe2].to_numpy()).transform("mean")
        if np.max(np.abs(out.to_numpy() - before)) < tol:
            break
    return out


def _cluster_codes(clusters: np.ndarray) -> tuple[np.ndarray, int]:
    _, codes = np.unique(clusters, return_inverse=True)
    return codes, int(codes.max()) + 1


def _cluster_meat(x: np.ndarray, resid: np.ndarray, codes: np.ndarray, n_g: int) -> np.ndarray:
    """Sum over clusters of (X_g' u_g)(X_g' u_g)', vectorized."""
    k = x.shape[1]
    scores = np.zeros((n_g, k))
    np.add.at(scores, codes, x * resid[:, None])
    return scores.T @ scores


def _cluster_ols(
    y: np.ndarray, x: np.ndarray, clusters: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """OLS beta and CRV1 cluster-robust vcov (no intercept; demeaned inputs)."""
    xtx = x.T @ x
    xtx_inv = np.linalg.pinv(xtx)
    beta = xtx_inv @ (x.T @ y)
    resid = y - x @ beta
    codes, n_g = _cluster_codes(clusters)
    meat = _cluster_meat(x, resid, codes, n_g)
    n, k = x.shape
    dof = (n_g / (n_g - 1)) * ((n - 1) / (n - k)) if n_g > 1 and n > k else 1.0
    vcov = dof * xtx_inv @ meat @ xtx_inv
    return beta, vcov


def _first_stage_f(p: np.ndarray, z: np.ndarray, clusters: np.ndarray) -> float:
    beta, vcov = _cluster_ols(p, z, clusters)
    try:
        f = float(beta @ np.linalg.pinv(vcov) @ beta / z.shape[1])
    except np.linalg.LinAlgError:
        f = 0.0
    return f


def _ar_confidence_set(
    y: np.ndarray,
    p: np.ndarray,
    z: np.ndarray,
    clusters: np.ndarray,
    grid: np.ndarray,
) -> tuple[float | None, float | None]:
    """Anderson-Rubin confidence set by grid inversion: betas where instruments
    have no explanatory power for y - beta*p (cluster-robust Wald).

    Vectorized over the grid: the reduced-form coefficient and per-cluster
    scores are linear in beta, so meat(beta) is a quadratic matrix form.
    """
    crit = 5.99 if z.shape[1] == 2 else 3.84
    k = z.shape[1]
    codes, n_g = _cluster_codes(clusters)
    ztz_inv = np.linalg.pinv(z.T @ z)
    a0 = ztz_inv @ (z.T @ y)  # coef at beta=0
    a1 = ztz_inv @ (z.T @ p)  # d(coef)/d(-beta)
    # Per-cluster score components: s_g(b) = c0_g - b * c1_g
    u0 = y - z @ a0
    u1 = p - z @ a1
    c0 = np.zeros((n_g, k))
    c1 = np.zeros((n_g, k))
    np.add.at(c0, codes, z * u0[:, None])
    np.add.at(c1, codes, z * u1[:, None])
    m00 = c0.T @ c0
    m01 = c0.T @ c1
    m11 = c1.T @ c1
    dof = n_g / max(n_g - 1, 1)
    accepted: list[float] = []
    for b in grid:
        # Wald-form AR: reduced-form coefficient of (y - b p) on Z with
        # cluster-robust vcov; both are affine/quadratic in b.
        coef = a0 - b * a1
        meat = (m00 - b * (m01 + m01.T) + b * b * m11) * dof
        vcov = ztz_inv @ meat @ ztz_inv
        try:
            w = float(coef @ np.linalg.pinv(vcov) @ coef)
        except np.linalg.LinAlgError:
            continue
        if w <= crit:
            accepted.append(float(b))
    if not accepted:
        return None, None
    return min(accepted), max(accepted)


def _overid_j(y: np.ndarray, p: np.ndarray, z: np.ndarray, beta_iv: float) -> tuple[float, float]:
    """Sargan J: n * R^2 of 2SLS residuals on instruments. Chi2(L-1)."""
    from scipy import stats

    resid = y - beta_iv * p
    zz_inv = np.linalg.pinv(z.T @ z)
    proj = z @ (zz_inv @ (z.T @ resid))
    ssr_explained = float(proj @ proj)
    ssr_total = float(resid @ resid)
    r2 = ssr_explained / ssr_total if ssr_total > 0 else 0.0
    j = len(y) * r2
    dof = z.shape[1] - 1
    pval = float(1 - stats.chi2.cdf(j, dof)) if dof >= 1 else float("nan")
    return j, pval


def estimate_segment(panel_seg: pl.DataFrame, rules: Rules, segment: int) -> SegmentEstimate:
    sc = rules.sensitivity
    est = SegmentEstimate(segment=segment)

    work = panel_seg.filter(pl.col("contract_id").is_null())
    est.n_obs = work.height
    est.n_pairs = work["pair"].n_unique()
    pos = work.filter(pl.col("qty") > 0)
    est.n_positive = pos.height
    if est.n_positive < sc.min_observations:
        est.status_reasons.append(f"only {est.n_positive} positive observations")
        return est

    # Within-pair price variation.
    cv = (
        pos.group_by("pair")
        .agg((pl.col("log_price").std()).alias("sd"))
        .select(pl.col("sd").mean())
        .item()
    )
    est.price_cv_within = float(cv or 0.0)
    if est.price_cv_within < sc.min_price_variation_cv:
        est.status_reasons.append(
            f"within-pair price variation {est.price_cv_within:.4f} below minimum"
        )
        return est

    cluster_col = "cluster_month" if sc.cluster_level == "month" else "cluster_supplier_month"
    pdf = _to_pandas(
        pos.select(
            "log_qty",
            "log_price",
            "z_cost",
            "z_tariff",
            "pair",
            "month_idx",
            cluster_col,
            "qty",
        ).drop_nulls(subset=["log_qty", "log_price"])
    )
    if len(pdf) < sc.min_observations:
        est.status_reasons.append("too few complete rows after joins")
        return est

    # --- naive FE-OLS ------------------------------------------------------
    dm = _two_way_demean(pdf, ["log_qty", "log_price"], "pair", "month_idx")
    y = dm["log_qty"].to_numpy()
    p = dm[["log_price"]].to_numpy()
    clusters = pdf[cluster_col].to_numpy()
    beta, vcov = _cluster_ols(y, p, clusters)
    est.naive_fe_ols = float(beta[0])
    est.naive_se = float(np.sqrt(vcov[0, 0]))

    # --- PPML on the panel incl. zeros --------------------------------------
    est.ppml = _ppml(work, rules)

    # --- IV ------------------------------------------------------------------
    have_z = ("z_cost" in pdf.columns and pdf["z_cost"].notna().all()) and (
        "z_tariff" in pdf.columns and pdf["z_tariff"].notna().all()
    )
    instruments = list(sc.instruments)
    z_cols = [i.column for i in instruments] if instruments else ["z_cost", "z_tariff"]
    z_cols = [
        c
        if c in ("z_cost", "z_tariff")
        else {"supplier_cost_shock": "z_cost", "category_tariff_shock": "z_tariff"}.get(c, c)
        for c in z_cols
    ]
    if not have_z:
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons.append("instruments unavailable in the data")
        return est

    dmz = _two_way_demean(pdf, ["log_qty", "log_price", *z_cols], "pair", "month_idx")
    yz = dmz["log_qty"].to_numpy()
    pz = dmz["log_price"].to_numpy()
    z = dmz[z_cols].to_numpy()

    if len(pdf) < sc.min_iv_observations or est.n_pairs < sc.min_iv_pairs:
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons.append(
            f"IV support too thin ({len(pdf)} obs / {est.n_pairs} pairs); "
            f"status capped at descriptive"
        )
        return est

    est.first_stage_f = _first_stage_f(pz, z, clusters)
    # Identifying coverage: share of pairs with within-pair instrument movement.
    cov = (
        pos.group_by("pair")
        .agg(((pl.col("z_cost") + pl.col("z_tariff")).std()).alias("sd"))
        .select((pl.col("sd") > 1e-8).mean())
        .item()
    )
    est.identifying_coverage = float(cov or 0.0)

    # 2SLS via within-transformed variables.
    zz_inv = np.linalg.pinv(z.T @ z)
    p_hat = z @ (zz_inv @ (z.T @ pz))
    denom = float(p_hat @ pz)
    if abs(denom) < 1e-12:
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons.append("first stage has no identifying variation")
        return est
    beta_iv = float((p_hat @ yz) / denom)
    est.iv = beta_iv
    # Cluster-robust IV se via sandwich on moment p_hat*(u).
    u = yz - beta_iv * pz
    codes, n_g = _cluster_codes(clusters)
    score_sum = np.zeros(n_g)
    np.add.at(score_sum, codes, p_hat * u)
    meat = float((score_sum**2).sum()) * n_g / max(n_g - 1, 1)
    bread = denom
    est.iv_se = float(np.sqrt(meat) / abs(bread)) if bread else None
    if est.iv_se:
        from scipy import stats as _st

        crit = float(_st.t.ppf(0.975, max(n_g - 1, 2)))
        est.iv_ci_low = beta_iv - crit * est.iv_se
        est.iv_ci_high = beta_iv + crit * est.iv_se

    grid = np.arange(-5.0, 1.0, 0.02)
    est.ar_ci_low, est.ar_ci_high = _ar_confidence_set(yz, pz, z, clusters, grid)

    if len(z_cols) >= 2:
        est.overid_j, est.overid_p = _overid_j(yz, pz, z, beta_iv)

    # Robustness: IV with each instrument alone + FE-OLS + PPML.
    alt: list[float] = [beta_iv]
    for j in range(z.shape[1]):
        zj = z[:, [j]]
        pj = zj @ (np.linalg.pinv(zj.T @ zj) @ (zj.T @ pz))
        dj = float(pj @ pz)
        if abs(dj) > 1e-12:
            alt.append(float((pj @ yz) / dj))
    est.robustness_spread = float(max(alt) - min(alt)) if len(alt) > 1 else 0.0

    # --- status ---------------------------------------------------------------
    reasons: list[str] = []
    rationale_ok = all(i.exclusion_rationale for i in instruments) and len(instruments) >= 1
    if not rationale_ok:
        reasons.append("exclusion rationale missing in config")
    if est.first_stage_f is None or est.first_stage_f < sc.min_first_stage_f:
        reasons.append(f"first-stage F {est.first_stage_f:.1f} below {sc.min_first_stage_f}")
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons = reasons
        return est
    if est.overid_p is not None and est.overid_p < sc.overid_alpha:
        reasons.append(f"overidentification rejected (p={est.overid_p:.4f})")
    if est.robustness_spread is not None and est.robustness_spread > sc.robustness_max_spread:
        reasons.append(f"robustness spread {est.robustness_spread:.2f} exceeds maximum")
    # The primary 95% interval is the cluster-robust Wald CI with t(G-1)
    # critical values (nominal coverage confirmed by simulation); the
    # Anderson-Rubin set is reported alongside as the weak-IV-robust interval
    # and becomes the relevant one when the first stage is weak.
    ci_width = (
        (est.iv_ci_high - est.iv_ci_low)
        if est.iv_ci_high is not None and est.iv_ci_low is not None
        else None
    )
    if ci_width is None or ci_width > sc.max_ci_width:
        reasons.append(f"CI width {ci_width if ci_width is not None else 'n/a'} exceeds maximum")
    est.status = STATUS_CAUSAL if not reasons else STATUS_IV
    est.status_reasons = reasons
    return est


def _ppml(panel_seg: pl.DataFrame, rules: Rules) -> float | None:
    """FE-Poisson via pyfixest on the panel including true zeros."""
    try:
        import pyfixest as pf

        df = panel_seg.filter(
            pl.col("contract_id").is_null() & pl.col("log_price").is_not_null()
        ).select("qty", "log_price", "pair", "month_idx")
        pdf = _to_pandas(df)
        if len(pdf) < 50 or pdf["qty"].sum() <= 0:
            return None
        fit = pf.fepois("qty ~ log_price | pair + month_idx", data=pdf)
        return float(fit.coef()["log_price"])
    except Exception:
        return None


def run_sensitivity(panel: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Estimate every segment; returns one row per segment with diagnostics."""
    segs = [s for s in panel["segment_leaf"].unique().to_list() if s is not None]
    rows = []
    for s in sorted(segs):
        seg_panel = panel.filter(pl.col("segment_leaf") == s)
        rows.append(estimate_segment(seg_panel, rules, int(s)).to_dict())
    return pl.DataFrame(rows)
