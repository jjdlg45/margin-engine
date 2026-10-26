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
from margin_engine.sensitivity.market_control import (
    CONTROL_ABSENT,
    ESTIMAND_DESCRIPTIVE,
    PATH_NOT_CONTROLLED,
    SOURCE_ABSENT,
    assess_market_control,
    estimand_for,
    resolve_competitor_path,
)

STATUS_INSUFFICIENT = "insufficient_data"
STATUS_DESCRIPTIVE = "descriptive_elasticity"
STATUS_IV = "iv_estimated_elasticity"
STATUS_CAUSAL = "causal_elasticity_supported"

GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class GateResult:
    """One causal criterion's outcome (Run 6, Phase 70). The spec's
    `causal_status_reasons`: machine-readable, ordered, each with a
    plain-English detail so the UI never has to parse prose."""

    order: int
    gate: str
    result: str
    detail: str


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
    # PPML on positive-quantity rows only (24.8): its gap to the full-panel
    # PPML reveals when carried-forward-price zero rows drive the estimate.
    ppml_positive: float | None = None
    carried_forward_share: float | None = None
    iv: float | None = None
    iv_se: float | None = None
    iv_ci_low: float | None = None
    iv_ci_high: float | None = None
    first_stage_f: float | None = None
    overid_j: float | None = None
    # Instruments still identifying after the market control (Phase 70).
    n_identified_instruments: int | None = None
    overid_p: float | None = None
    ar_ci_low: float | None = None
    ar_ci_high: float | None = None
    ar_bounded: bool | None = None
    robustness_spread: float | None = None
    # How many IDENTIFIED specifications the spread covers, and which
    # alternatives were excluded as unidentified (Run 6, Phase 70).
    robustness_n_specifications: int | None = None
    robustness_excluded_weak: str = ""
    identifying_coverage: float | None = None
    # Identifying price support (24.6): 5th-95th percentile of the
    # instrument-predicted within log-price movement. Proposed moves outside
    # this range are extrapolation and get no volume response downstream.
    support_log_lo: float | None = None
    support_log_hi: float | None = None
    market_control_used: bool = False
    # Competitor-price path (Run 6, Phase 70). The estimand is named, the
    # control's adequacy statistics are reported, and the first stage is
    # reported both before and after the control so the cost of controlling is
    # visible rather than implied.
    estimand: str = ESTIMAND_DESCRIPTIVE
    estimand_bias_direction: str | None = None
    market_control_status: str = CONTROL_ABSENT
    market_control_coverage: float | None = None
    market_control_lag_periods: int | None = None
    market_control_within_cell_values: float | None = None
    market_control_price_r2: float | None = None
    market_control_instrument_r2: float | None = None
    market_control_detail: str = ""
    competitor_path_resolution: str = PATH_NOT_CONTROLLED
    competitor_path_detail: str = ""
    first_stage_f_uncontrolled: float | None = None
    status: str = STATUS_INSUFFICIENT
    status_reasons: list[str] = field(default_factory=list)
    # Machine-readable per-gate record (the spec's `causal_status_reasons`):
    # every causal criterion with pass / fail / not evaluated and a
    # plain-English detail, in a stable order for the UI and the exports.
    causal_status_reasons: list[GateResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Flat row for the CSV/Parquet exports: the gate list rides its own
        long-form export (a List/Struct column breaks the CSV sink), with the
        failed codes kept here as a pipe-joined string."""
        d = self.__dict__.copy()
        d["status_reasons"] = "; ".join(self.status_reasons)
        gates = d.pop("causal_status_reasons", [])
        d["causal_gate_fail_codes"] = "|".join(g.gate for g in gates if g.result == GATE_FAIL)
        return d

    def gate_rows(self) -> list[dict[str, Any]]:
        """Long-form rows for the `sensitivity_gates` export."""
        return [
            {
                "segment": self.segment,
                "gate_order": g.order,
                "gate": g.gate,
                "result": g.result,
                "detail": g.detail,
            }
            for g in self.causal_status_reasons
        ]


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


def _resid(v: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Residualize v on a single column m (Frisch-Waugh). A degenerate control
    (no variation) returns v unchanged - the adequacy assessment has already
    refused it as absorbed, so this is never a silent pass."""
    denom = float(m @ m)
    if denom <= 1e-12:
        return v
    return v - m * (float(m @ v) / denom)


def _cluster_codes(clusters: np.ndarray) -> tuple[np.ndarray, int]:
    _, codes = np.unique(clusters, return_inverse=True)
    return codes, int(codes.max()) + 1


# A multiway cluster specification: (codes, n_groups, sign). One entry for
# one-way clustering; for two-way (Cameron-Gelbach-Miller) three entries:
# dimension A (+1), dimension B (+1), intersection A x B (-1).
ClusterSpec = list[tuple[np.ndarray, int, int]]


def _make_cluster_spec(*dims: np.ndarray) -> ClusterSpec:
    """One-way spec for a single dimension; CGM two-way spec for two."""
    if len(dims) == 1:
        codes, n_g = _cluster_codes(dims[0])
        return [(codes, n_g, +1)]
    a, b = dims
    ca, na = _cluster_codes(a)
    cb, nb = _cluster_codes(b)
    inter = np.char.add(np.char.add(a.astype(str), "|"), b.astype(str))
    ci, ni = _cluster_codes(inter)
    return [(ca, na, +1), (cb, nb, +1), (ci, ni, -1)]


def _spec_min_groups(spec: ClusterSpec) -> int:
    """Degrees of freedom driver: the smallest positive-sign group count."""
    return min(n_g for _, n_g, sign in spec if sign > 0)


def _cluster_meat(x: np.ndarray, resid: np.ndarray, codes: np.ndarray, n_g: int) -> np.ndarray:
    """Sum over clusters of (X_g' u_g)(X_g' u_g)', vectorized."""
    k = x.shape[1]
    scores = np.zeros((n_g, k))
    np.add.at(scores, codes, x * resid[:, None])
    return scores.T @ scores


def _cluster_ols(y: np.ndarray, x: np.ndarray, spec: ClusterSpec) -> tuple[np.ndarray, np.ndarray]:
    """OLS beta and CRV1 cluster-robust vcov (no intercept; demeaned inputs).
    Multiway vcov combines per-dimension sandwiches with CGM signs; a
    non-positive-definite combination falls back to the largest one-way
    component (standard CGM practice for the scalar case)."""
    xtx = x.T @ x
    xtx_inv = np.linalg.pinv(xtx)
    beta = xtx_inv @ (x.T @ y)
    resid = y - x @ beta
    n, k = x.shape
    parts: list[np.ndarray] = []
    for codes, n_g, sign in spec:
        meat = _cluster_meat(x, resid, codes, n_g)
        dof = (n_g / (n_g - 1)) * ((n - 1) / (n - k)) if n_g > 1 and n > k else 1.0
        parts.append(sign * dof * xtx_inv @ meat @ xtx_inv)
    vcov = np.sum(np.stack(parts), axis=0)
    if k == 1 and float(vcov[0, 0]) <= 0 and len(parts) > 1:
        # Standard CGM practice for the scalar case: fall back to the largest
        # positive one-way component. When NO component is positive the
        # clustered variance is not estimable, so it is reported as infinite -
        # which fails the strength and precision gates instead of passing them
        # on a variance of zero.
        positives = [float(p[0, 0]) for p in parts if float(p[0, 0]) > 0]
        vcov = np.array([[max(positives)]]) if positives else np.array([[np.inf]])
    return beta, vcov


def _first_stage_f(p: np.ndarray, z: np.ndarray, spec: ClusterSpec) -> float:
    beta, vcov = _cluster_ols(p, z, spec)
    if not np.all(np.isfinite(vcov)):
        # An inestimable clustered variance is no evidence of strength, so it
        # must read as a WEAK first stage rather than passing the gate.
        return 0.0
    try:
        f = float(beta @ np.linalg.pinv(vcov) @ beta / z.shape[1])
    except np.linalg.LinAlgError:
        f = 0.0
    return 0.0 if not np.isfinite(f) else f


def _ar_confidence_set(
    y: np.ndarray,
    p: np.ndarray,
    z: np.ndarray,
    spec: ClusterSpec,
    grid: np.ndarray,
) -> tuple[float | None, float | None]:
    """Anderson-Rubin confidence set by grid inversion: betas where instruments
    have no explanatory power for y - beta*p (cluster-robust Wald, multiway
    via CGM signs).

    Vectorized over the grid: the reduced-form coefficient and per-cluster
    scores are linear in beta, so meat(beta) is a quadratic matrix form.
    """
    crit = 5.99 if z.shape[1] == 2 else 3.84
    k = z.shape[1]
    ztz_inv = np.linalg.pinv(z.T @ z)
    a0 = ztz_inv @ (z.T @ y)  # coef at beta=0
    a1 = ztz_inv @ (z.T @ p)  # d(coef)/d(-beta)
    # Per-cluster score components: s_g(b) = c0_g - b * c1_g, per spec entry.
    u0 = y - z @ a0
    u1 = p - z @ a1
    terms: list[tuple[float, np.ndarray, np.ndarray, np.ndarray]] = []
    for codes, n_g, sign in spec:
        c0 = np.zeros((n_g, k))
        c1 = np.zeros((n_g, k))
        np.add.at(c0, codes, z * u0[:, None])
        np.add.at(c1, codes, z * u1[:, None])
        dof = sign * n_g / max(n_g - 1, 1)
        terms.append((dof, c0.T @ c0, c0.T @ c1, c1.T @ c1))
    accepted: list[float] = []
    for b in grid:
        # Wald-form AR: reduced-form coefficient of (y - b p) on Z with
        # cluster-robust vcov; both are affine/quadratic in b.
        coef = a0 - b * a1
        meat = sum(d * (m00 - b * (m01 + m01.T) + b * b * m11) for d, m00, m01, m11 in terms)
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


def estimate_segment(
    panel_seg: pl.DataFrame,
    rules: Rules,
    segment: int,
    *,
    market_control_source: str = SOURCE_ABSENT,
    market_control_grain: str | None = None,
) -> SegmentEstimate:
    """`market_control_source` is the public-source decision from
    `select_market_index` (Run 6, Phase 70) and `market_control_grain` the
    index's join grain (e.g. "item_category:month"). Passing the default means
    no control is in evidence, which caps the status - never a silent pass.
    """
    sc = rules.sensitivity
    est = SegmentEstimate(segment=segment)

    work = panel_seg.filter(pl.col("contract_id").is_null())
    est.n_obs = work.height
    est.n_pairs = work["pair"].n_unique()
    pos = work.filter(pl.col("qty") > 0)
    est.n_positive = pos.height
    if "price_carried_forward" in work.columns and work.height:
        est.carried_forward_share = float(work["price_carried_forward"].mean() or 0.0)  # type: ignore[arg-type]
    if est.n_positive < sc.min_observations:
        est.status_reasons.append(f"only {est.n_positive} positive observations")
        return est

    # Within-pair price variation.
    cv = (
        pos.group_by("pair")
        .agg((pl.col("log_price").std()).alias("sd"))
        # group_by output order is not deterministic; sort before the float
        # mean so the summation order (and the last ulp) is reproducible.
        .sort("pair")
        .select(pl.col("sd").mean())
        .item()
    )
    est.price_cv_within = float(cv or 0.0)
    if est.price_cv_within < sc.min_price_variation_cv:
        est.status_reasons.append(
            f"within-pair price variation {est.price_cv_within:.4f} below minimum"
        )
        return est

    if sc.cluster_level == "month":
        cluster_cols = ["cluster_month"]
    elif sc.cluster_level == "month_customer":
        cluster_cols = ["cluster_month", "cluster_customer"]
    else:
        cluster_cols = ["cluster_supplier_month"]
    # Time fixed effects at item-role x month when roles are known: key value
    # items are broadly co-carried and co-move within a month, and absorbing
    # that common component keeps cluster-robust standard errors honest.
    time_fe = "role_month" if "role_month" in pos.columns else "month_idx"
    # Market/competitor price index control (24.10, gated in Run 6 Phase 70).
    # Coverage is measured here, on this segment's own estimation rows; a
    # partially covered index is INCOMPLETE rather than silently switched off
    # (the pre-Phase-70 code dropped the control on any null with no reason
    # recorded and still reported it as used).
    mkt_coverage: float | None = None
    mkt_lag: int | None = None
    if "log_mkt" in pos.columns and pos.height:
        mkt_coverage = float(1.0 - (pos["log_mkt"].null_count() / pos.height))
        covered = pos.filter(pl.col("log_mkt").is_not_null())
        if covered.height:
            _last_all = pos["month_idx"].max()
            _last_cov = covered["month_idx"].max()
            if _last_all is not None and _last_cov is not None:
                mkt_lag = int(_last_all) - int(_last_cov)  # type: ignore[arg-type]
    has_mkt = "log_mkt" in pos.columns and pos["log_mkt"].null_count() == 0
    # Instruments can be absent entirely (no supplier/tariff reference data):
    # select them only when the panel carries them; `have_z` below caps the
    # status at descriptive in that case.
    z_present = [c for c in ("z_cost", "z_tariff") if c in pos.columns]
    pdf = _to_pandas(
        pos.select(
            "log_qty",
            "log_price",
            *z_present,
            "pair",
            "month_idx",
            *cluster_cols,
            "qty",
            *([time_fe] if time_fe != "month_idx" else []),
            *(["log_mkt"] if has_mkt else []),
        ).drop_nulls(subset=["log_qty", "log_price"])
    )
    if len(pdf) < sc.min_observations:
        est.status_reasons.append("too few complete rows after joins")
        return est

    spec = _make_cluster_spec(*(pdf[c].to_numpy() for c in cluster_cols))

    # --- naive FE-OLS ------------------------------------------------------
    # The naive estimate documents endogeneity bias, so it is reported on the
    # SAME design as the headline: controlled when the control is usable.
    dm_cols = ["log_qty", "log_price"] + (["log_mkt"] if has_mkt else [])
    dm = _two_way_demean(pdf, dm_cols, "pair", time_fe)
    y = dm["log_qty"].to_numpy()
    p_raw = dm["log_price"].to_numpy()
    if has_mkt:
        m_naive = dm["log_mkt"].to_numpy()
        y = _resid(y, m_naive)
        p_raw = _resid(p_raw, m_naive)
    p = p_raw.reshape(-1, 1)
    beta, vcov = _cluster_ols(y, p, spec)
    est.naive_fe_ols = float(beta[0])
    est.naive_se = float(np.sqrt(vcov[0, 0]))

    # --- PPML on the panel incl. zeros --------------------------------------
    est.ppml = _ppml(work, rules)
    # Robustness against carried-forward zero rows (24.8): the same PPML on
    # positive-quantity rows only; the gap joins the robustness criterion.
    est.ppml_positive = _ppml(pos, rules)

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
        est.estimand, est.estimand_bias_direction = estimand_for(iv_ran=False, path_resolved=False)
        return est

    dmz = _two_way_demean(
        pdf, ["log_qty", "log_price", *z_cols] + (["log_mkt"] if has_mkt else []), "pair", time_fe
    )
    # --- market control adequacy (Run 6, Phase 70) ---------------------------
    # Assessed on the within-transformed sample BEFORE partialling, because
    # that is the space the control operates in, and reported in full.
    yz_raw = dmz["log_qty"].to_numpy()
    pz_raw = dmz["log_price"].to_numpy()
    z_raw = dmz[z_cols].to_numpy()
    mkt_raw = dmz["log_mkt"].to_numpy() if has_mkt else None
    time_cell = pdf[time_fe].to_numpy() if time_fe in pdf.columns else pdf["month_idx"].to_numpy()
    assessment = assess_market_control(
        source=market_control_source,
        log_mkt=mkt_raw,
        log_price_within=pz_raw,
        instruments_within=z_raw,
        time_cell=time_cell,
        coverage=mkt_coverage,
        lag_periods=mkt_lag,
        rules=rules,
    )
    est.market_control_status = assessment.status
    est.market_control_coverage = assessment.coverage
    est.market_control_lag_periods = assessment.lag_periods
    est.market_control_within_cell_values = assessment.within_cell_values
    est.market_control_price_r2 = assessment.price_relevance_r2
    est.market_control_instrument_r2 = assessment.instrument_r2
    est.market_control_detail = assessment.detail
    use_control = bool(has_mkt and assessment.usable)
    est.market_control_used = use_control
    # The first stage WITHOUT the control, so the cost of controlling is
    # visible rather than implied.
    est.first_stage_f_uncontrolled = _first_stage_f(pz_raw, z_raw, spec)

    if use_control and mkt_raw is not None:
        yz = _resid(yz_raw, mkt_raw)
        pz = _resid(pz_raw, mkt_raw)
        z = np.column_stack([_resid(z_raw[:, j], mkt_raw) for j in range(z_raw.shape[1])])
        mkt_within: np.ndarray | None = mkt_raw
    else:
        yz, pz, z = yz_raw, pz_raw, z_raw
        mkt_within = None

    if len(pdf) < sc.min_iv_observations or est.n_pairs < sc.min_iv_pairs:
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons.append(
            f"IV support too thin ({len(pdf)} obs / {est.n_pairs} pairs); "
            f"status capped at descriptive"
        )
        est.estimand, est.estimand_bias_direction = estimand_for(iv_ran=False, path_resolved=False)
        return est

    est.first_stage_f = _first_stage_f(pz, z, spec)
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
        est.estimand, est.estimand_bias_direction = estimand_for(iv_ran=False, path_resolved=False)
        return est
    beta_iv = float((p_hat @ yz) / denom)
    est.iv = beta_iv
    # Identifying price support (24.6): the range of instrument-predicted
    # within log-price movement. Downstream, proposed moves outside this range
    # are extrapolation beyond what the instruments ever induced.
    est.support_log_lo = float(np.quantile(p_hat, 0.05))
    est.support_log_hi = float(np.quantile(p_hat, 0.95))

    # Full cluster-jackknife IV standard error: leave-one-cluster-out betas
    # with the FIRST STAGE re-estimated each time. Fixed-projection sandwiches
    # (CRV1 and partial jackknives) ignore first-stage sampling noise, which is
    # non-negligible in thinner segments and was the source of under-coverage
    # in simulation. For two-way clustering the variance combines per-dimension
    # jackknives with CGM signs: V = V_A + V_B - V_AxB (24.5); a non-positive
    # combination falls back to the larger one-way variance.
    def _jackknife_var(codes: np.ndarray, n_g: int) -> float | None:
        """Leave-one-cluster-out IV variance with the market control RE-FITTED
        in every replicate (Run 6, Phase 70).

        Partialling the control out once on the full sample and then
        jackknifing the residuals treats the partialling coefficient as known
        and understates the variance by the control's own sampling
        contribution. Here the Frisch-Waugh residualization is redone inside
        each replicate. It stays exact and cheap because every moment needed
        is a leave-one-out sum: with s = control, the residualized moments are
            Z'y - (Z's)(s'y)/(s's),   Z'p - (Z's)(s'p)/(s's)
        all taken over the retained rows, so per-cluster partial sums suffice
        and no re-regression loop is required.
        """
        k_z = z.shape[1]
        zz_g = np.zeros((n_g, k_z, k_z))
        zp_g = np.zeros((n_g, k_z))
        zy_g = np.zeros((n_g, k_z))
        has_ctl = mkt_within is not None
        zs_g = np.zeros((n_g, k_z))
        ss_g = np.zeros(n_g)
        sy_g = np.zeros(n_g)
        sp_g = np.zeros(n_g)
        for g in range(n_g):
            m = codes == g
            zg = z[m]
            zz_g[g] = zg.T @ zg
            zp_g[g] = zg.T @ pz[m]
            zy_g[g] = zg.T @ yz[m]
            if has_ctl:
                sg = mkt_within[m]  # type: ignore[index]
                zs_g[g] = zg.T @ sg
                ss_g[g] = float(sg @ sg)
                sy_g[g] = float(sg @ yz[m])
                sp_g[g] = float(sg @ pz[m])
        zz_tot = zz_g.sum(axis=0)
        zp_tot = zp_g.sum(axis=0)
        zy_tot = zy_g.sum(axis=0)
        zs_tot, ss_tot, sy_tot, sp_tot = (
            zs_g.sum(axis=0),
            float(ss_g.sum()),
            float(sy_g.sum()),
            float(sp_g.sum()),
        )
        loco_list = []
        for g in range(n_g):
            zz = zz_tot - zz_g[g]
            bz = zp_tot - zp_g[g]
            by = zy_tot - zy_g[g]
            if has_ctl:
                zs = zs_tot - zs_g[g]
                ss = ss_tot - ss_g[g]
                if ss > 1e-12:
                    # Residualize y, p and Z on the control within this
                    # replicate (FWL), re-estimating its coefficient here.
                    zz = zz - np.outer(zs, zs) / ss
                    bz = bz - zs * ((sp_tot - sp_g[g]) / ss)
                    by = by - zs * ((sy_tot - sy_g[g]) / ss)
            a = np.linalg.pinv(zz)
            d = float(bz @ a @ bz)
            if abs(d) > 1e-12:
                loco_list.append(float(bz @ a @ by) / d)
        if len(loco_list) < 2:
            return None
        loco = np.array(loco_list)
        n_l = len(loco)
        return float((n_l - 1) / n_l * ((loco - loco.mean()) ** 2).sum())

    jk_vals: list[tuple[float, int]] = []
    jk_ok = True
    for codes, n_g, sign in spec:
        v = _jackknife_var(codes, n_g)
        if v is None:
            jk_ok = False
            break
        jk_vals.append((v, sign))
    iv_var: float | None = None
    if jk_ok:
        combined = sum(s * v for v, s in jk_vals)
        if combined <= 0 and len(jk_vals) > 1:
            positives = [v for v, s in jk_vals if s > 0 and v > 0]
            combined = max(positives) if positives else 0.0
        if combined > 0:
            iv_var = float(combined)
    est.iv_se = float(np.sqrt(iv_var)) if iv_var is not None else None
    if est.iv_se:
        from scipy import stats as _st

        crit = float(_st.t.ppf(0.975, max(_spec_min_groups(spec) - 1, 2)))
        est.iv_ci_low = beta_iv - crit * est.iv_se
        est.iv_ci_high = beta_iv + crit * est.iv_se

    grid = np.arange(-5.0, 1.0, 0.02)
    est.ar_ci_low, est.ar_ci_high = _ar_confidence_set(yz, pz, z, spec, grid)
    # Bounded means the accepted set does not run into the search grid's edges.
    est.ar_bounded = (
        est.ar_ci_low is not None
        and est.ar_ci_high is not None
        and est.ar_ci_low > float(grid[0]) + 0.021
        and est.ar_ci_high < float(grid[-1]) - 0.021
    )

    # Overidentification over the instruments that are still IDENTIFIED after
    # controlling (Run 6, Phase 70). A J test run against an instrument with
    # no remaining variation tests nothing: its p-value would be noise
    # presented as agreement. With fewer than two identified instruments the
    # model is just-identified and the test is reported as not evaluated -
    # which the spec's own criterion contemplates ("when 2 instruments").
    identified_cols = [
        j for j in range(z.shape[1]) if _first_stage_f(pz, z[:, [j]], spec) >= sc.min_first_stage_f
    ]
    est.n_identified_instruments = len(identified_cols)
    if len(identified_cols) >= 2:
        est.overid_j, est.overid_p = _overid_j(yz, pz, z[:, identified_cols], beta_iv)

    # Robustness: the joint IV against each instrument ALONE - but only over
    # alternatives that are themselves identified (Run 6, Phase 70).
    #
    # Why the filter exists, measured not assumed: holding the category market
    # price constant removes nearly all of a CATEGORY-level tariff
    # instrument's variation (its own first stage falls to F < 6 on the demo
    # world), because a category tariff IS a category market-price mover. What
    # still identifies a unilateral response is the SUPPLIER-specific cost
    # shock, which moves this distributor's cost without moving the category
    # market price. A single-instrument specification whose own first stage is
    # weak is not evidence that the headline is unstable - it is an
    # unidentified specification, and judging the headline by it would report
    # weak-instrument noise as instability. Excluded alternatives are counted
    # and named, never dropped silently.
    alt: list[float] = [beta_iv]
    weak_alts: list[str] = []
    for j in range(z.shape[1]):
        zj = z[:, [j]]
        f_j = _first_stage_f(pz, zj, spec)
        name = z_cols[j] if j < len(z_cols) else f"instrument {j + 1}"
        if f_j < sc.min_first_stage_f:
            weak_alts.append(f"{name} (own first-stage F {f_j:.1f})")
            continue
        pj = zj @ (np.linalg.pinv(zj.T @ zj) @ (zj.T @ pz))
        dj = float(pj @ pz)
        if abs(dj) > 1e-12:
            alt.append(float((pj @ yz) / dj))
    est.robustness_n_specifications = len(alt)
    est.robustness_excluded_weak = "; ".join(weak_alts)
    # With no identified alternative there is nothing to compare the headline
    # against: the spread stays UNKNOWN rather than collapsing to a
    # reassuring zero.
    est.robustness_spread = float(max(alt) - min(alt)) if len(alt) > 1 else None

    # --- status ---------------------------------------------------------------
    reasons: list[str] = []
    gates: list[GateResult] = []

    def gate(order: int, name: str, ok: bool | None, detail: str) -> None:
        """Record one criterion. `ok=None` means it could not be evaluated -
        never reported as a pass."""
        gates.append(
            GateResult(
                order=order,
                gate=name,
                result=GATE_NOT_EVALUATED if ok is None else (GATE_PASS if ok else GATE_FAIL),
                detail=detail,
            )
        )

    rationale_ok = all(i.exclusion_rationale for i in instruments) and len(instruments) >= 1
    gate(
        1,
        "exclusion_rationale_documented",
        rationale_ok,
        "every instrument carries a written exclusion argument in config"
        if rationale_ok
        else "at least one instrument has no documented exclusion rationale",
    )
    if not rationale_ok:
        reasons.append("exclusion rationale missing in config")

    # Competitor-price path (Run 6, Phase 70): the estimand is a UNILATERAL
    # move, so industry-wide cost instruments need competitor-price conditions
    # held constant - by an adequate public index, or by a documented argument
    # that the instrument has no such path.
    path = resolve_competitor_path(
        tuple(instruments),
        control_adequate=assessment.adequate,
        control_grain=market_control_grain,
    )
    est.competitor_path_resolution = path.code
    est.competitor_path_detail = path.detail
    gate(2, "competitor_price_path_addressed", path.resolved, path.detail)
    if not path.resolved:
        reasons.append(f"competitor-price path not controlled: {path.detail}")

    f_ok = est.first_stage_f is not None and est.first_stage_f >= sc.min_first_stage_f
    gate(
        3,
        "first_stage_strength",
        f_ok,
        (
            f"first-stage F {est.first_stage_f:.1f} against the {sc.min_first_stage_f} "
            f"threshold (uncontrolled F {est.first_stage_f_uncontrolled:.1f})"
            if est.first_stage_f is not None and est.first_stage_f_uncontrolled is not None
            else "the first stage could not be computed"
        ),
    )
    if not f_ok:
        f_txt = f"{est.first_stage_f:.1f}" if est.first_stage_f is not None else "n/a"
        reasons.append(f"first-stage F {f_txt} below {sc.min_first_stage_f}")
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons = reasons
        est.causal_status_reasons = gates
        est.estimand, est.estimand_bias_direction = estimand_for(
            iv_ran=False, path_resolved=path.resolved
        )
        return est
    overid_ok = est.overid_p is None or est.overid_p >= sc.overid_alpha
    gate(
        4,
        "overidentification_not_rejected",
        None if est.overid_p is None else overid_ok,
        (
            f"overidentification test p={est.overid_p:.4f} against alpha {sc.overid_alpha} "
            f"over {est.n_identified_instruments} identified instruments; it cannot detect "
            "a violation those instruments share"
            if est.overid_p is not None
            else (
                f"only {est.n_identified_instruments or 0} instrument(s) still identify "
                "price after the market control, so the model is just-identified and "
                "there is nothing to overidentify"
            )
        ),
    )
    if est.overid_p is not None and est.overid_p < sc.overid_alpha:
        reasons.append(f"overidentification rejected (p={est.overid_p:.4f})")
    excluded_txt = (
        f"; excluded as unidentified: {est.robustness_excluded_weak}"
        if est.robustness_excluded_weak
        else ""
    )
    if est.robustness_spread is None:
        # No identified alternative exists, so the headline has nothing to be
        # checked against. That is not a pass.
        gate(
            5,
            "robust_across_specifications",
            None,
            "no alternative specification is identified, so the headline estimate "
            "could not be checked against one" + excluded_txt,
        )
        reasons.append(
            "robustness could not be assessed: no alternative specification is "
            "identified" + excluded_txt
        )
    else:
        spread_ok = est.robustness_spread <= sc.robustness_max_spread
        gate(
            5,
            "robust_across_specifications",
            spread_ok,
            f"estimates span {est.robustness_spread:.2f} across "
            f"{est.robustness_n_specifications} identified specifications against a "
            f"{sc.robustness_max_spread} maximum" + excluded_txt,
        )
        if not spread_ok:
            reasons.append(f"robustness spread {est.robustness_spread:.2f} exceeds maximum")
    # Carried-forward-price robustness (24.8): the PPML gap between the full
    # panel (zeros at carried-forward prices) and positive rows only.
    cf_gap = (
        abs(est.ppml - est.ppml_positive)
        if est.ppml is not None and est.ppml_positive is not None
        else None
    )
    gate(
        6,
        "carried_forward_price_robustness",
        None if cf_gap is None else cf_gap <= sc.robustness_max_spread,
        f"the PPML estimate moves {cf_gap:.2f} between the full panel (zeros at "
        f"carried-forward prices) and positive rows only, against a "
        f"{sc.robustness_max_spread} maximum; note PPML is fitted WITHOUT the "
        f"market control, so this compares a controlled headline to uncontrolled fits"
        if cf_gap is not None
        else "one of the two PPML fits did not converge, so the gap is unknown",
    )
    if cf_gap is not None and cf_gap > sc.robustness_max_spread:
        reasons.append(
            f"carried-forward robustness gap {cf_gap:.2f} exceeds maximum "
            f"(PPML with zeros {est.ppml:.2f} vs positive-only {est.ppml_positive:.2f})"
        )
    # The primary 95% interval is the cluster-robust Wald CI with t(G-1)
    # critical values (nominal coverage confirmed by simulation); the
    # Anderson-Rubin set is reported alongside as the weak-IV-robust interval
    # and becomes the relevant one when the first stage is weak.
    ci_width = (
        (est.iv_ci_high - est.iv_ci_low)
        if est.iv_ci_high is not None and est.iv_ci_low is not None
        else None
    )
    gate(
        7,
        "interval_precise_enough",
        None if ci_width is None else ci_width <= sc.max_ci_width,
        f"the 95% interval spans {ci_width:.2f} against a {sc.max_ci_width} maximum"
        if ci_width is not None
        else "the primary interval could not be computed",
    )
    if ci_width is None or ci_width > sc.max_ci_width:
        reasons.append(f"CI width {ci_width if ci_width is not None else 'n/a'} exceeds maximum")
    # Weak-IV-robust agreement (24.7): the top status also requires the
    # Anderson-Rubin set to be bounded and no wider than the configured
    # multiple of the primary interval.
    if not est.ar_bounded:
        if est.ar_ci_low is None:
            reasons.append(
                "Anderson-Rubin set empty (the instruments jointly reject every "
                "candidate elasticity: evidence of misspecification)"
            )
        else:
            reasons.append("Anderson-Rubin set unbounded (weak-IV-robust evidence insufficient)")
    elif ci_width is not None and est.ar_ci_low is not None and est.ar_ci_high is not None:
        ar_width = est.ar_ci_high - est.ar_ci_low
        if ar_width > sc.ar_max_width_ratio * ci_width:
            reasons.append(
                f"Anderson-Rubin set width {ar_width:.2f} exceeds "
                f"{sc.ar_max_width_ratio}x the primary interval ({ci_width:.2f})"
            )
    ar_detail = (
        "the weak-instrument-robust set is bounded and agrees with the primary interval"
        if est.ar_bounded
        else (
            "the weak-instrument-robust set is empty: the instruments jointly reject "
            "every candidate elasticity, which is evidence of misspecification"
            if est.ar_ci_low is None
            else "the weak-instrument-robust set runs to the edge of the search range, "
            "so it is reported as unbounded rather than compressed into an interval"
        )
    )
    ar_ok = bool(est.ar_bounded)
    if ar_ok and ci_width is not None and est.ar_ci_low is not None and est.ar_ci_high is not None:
        ar_ok = (est.ar_ci_high - est.ar_ci_low) <= sc.ar_max_width_ratio * ci_width
        if not ar_ok:
            ar_detail = (
                f"the weak-instrument-robust set spans "
                f"{est.ar_ci_high - est.ar_ci_low:.2f}, beyond {sc.ar_max_width_ratio}x "
                f"the primary interval ({ci_width:.2f})"
            )
    gate(8, "weak_instrument_robust_agreement", ar_ok, ar_detail)

    est.status = STATUS_CAUSAL if not reasons else STATUS_IV
    est.status_reasons = reasons
    est.causal_status_reasons = gates
    est.estimand, est.estimand_bias_direction = estimand_for(
        iv_ran=True, path_resolved=path.resolved
    )
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


@dataclass
class SensitivityResult:
    """Per-segment estimates plus the long-form causal-gate record (Run 6,
    Phase 70): one row per segment and criterion, so the UI and the exports
    read machine-readable outcomes instead of parsing prose."""

    estimates: pl.DataFrame
    gates: pl.DataFrame


GATES_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "segment": pl.Int64,
    "gate_order": pl.Int64,
    "gate": pl.String,
    "result": pl.String,
    "detail": pl.String,
}


def run_sensitivity_full(
    panel: pl.DataFrame,
    rules: Rules,
    *,
    market_control_source: str = SOURCE_ABSENT,
    market_control_grain: str | None = None,
) -> SensitivityResult:
    """Estimate every segment; returns the estimates and the gate record."""
    segs = [s for s in panel["segment_leaf"].unique().to_list() if s is not None]
    rows = []
    gate_rows: list[dict[str, Any]] = []
    for s in sorted(segs):
        seg_panel = panel.filter(pl.col("segment_leaf") == s)
        est = estimate_segment(
            seg_panel,
            rules,
            int(s),
            market_control_source=market_control_source,
            market_control_grain=market_control_grain,
        )
        rows.append(est.to_dict())
        gate_rows.extend(est.gate_rows())
    gates = (
        pl.DataFrame(gate_rows, schema=GATES_SCHEMA).sort("segment", "gate_order")
        if gate_rows
        else pl.DataFrame(schema=GATES_SCHEMA)
    )
    return SensitivityResult(estimates=pl.DataFrame(rows), gates=gates)


def run_sensitivity(
    panel: pl.DataFrame,
    rules: Rules,
    *,
    market_control_source: str = SOURCE_ABSENT,
    market_control_grain: str | None = None,
) -> pl.DataFrame:
    """Estimates only (the long-standing signature)."""
    return run_sensitivity_full(
        panel,
        rules,
        market_control_source=market_control_source,
        market_control_grain=market_control_grain,
    ).estimates
