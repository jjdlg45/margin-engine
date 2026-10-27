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

from collections.abc import Sequence
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
from margin_engine.sensitivity.weak_iv import (
    AR_SHAPE_EMPTY,
    AR_SHAPE_INTERVAL,
    LEAD_IMMATERIAL,
    LEAD_INCONCLUSIVE,
    LEAD_PASSED,
    LEAD_REJECTED,
    LEAD_UNAVAILABLE,
    PLACEBO_NOT_RUN,
    LeadTest,
    ar_set_from_accepted,
    effective_first_stage_f,
    lead_test_from_moments,
    placebo_verdict,
)

STATUS_INSUFFICIENT = "insufficient_data"
STATUS_DESCRIPTIVE = "descriptive_elasticity"
STATUS_IV = "iv_estimated_elasticity"
STATUS_CAUSAL = "causal_elasticity_supported"

# The TOTAL-quantity ladder (Run 6, Phase 72). Deliberately has no causal tier.
# Measured on planted worlds that contain a real price-driven extensive margin,
# the estimator tracks the total elasticity (median absolute error 0.149 against
# 0.349 when scored against the conditional truth, so it is genuinely capturing
# the extensive margin) but roughly six times less accurately than the
# conditional estimator manages on its own estimand (0.023 median, 0.094 at the
# 90th percentile). It also has no overidentification test and no out-of-sample
# backtest. A claim that outranks the conditional estimate cannot rest on that,
# so the ceiling is the instrumented tier and the engine never prices from it.
STATUS_TOTAL_INSUFFICIENT = "insufficient_data_total_quantity"
STATUS_TOTAL_DESCRIPTIVE = "descriptive_total_quantity"
STATUS_TOTAL_IV = "iv_estimated_total_quantity"

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
    # Total-quantity estimand (Run 6, Phase 72): the response of expected
    # quantity INCLUDING whether the customer buys at all, from an IV-Poisson
    # control function on the panel with its zero rows.
    iv_ppml: float | None = None
    iv_ppml_status: str = STATUS_TOTAL_INSUFFICIENT
    iv_ppml_reasons: str = ""
    iv_ppml_first_stage_f: float | None = None
    iv_ppml_rho_t: float | None = None
    iv_ppml_n_rows: int | None = None
    iv_ppml_detail: str = ""
    # Inference on the total-quantity estimate (Run 6, Phase 72). Two standard
    # errors, because they cover different things: the analytic one is
    # cluster-robust but treats the estimated control function as known, and
    # the resampled one refits the first stage per replicate but cannot
    # resample the period level the instrument is assigned at. Both are
    # published with what each omits.
    iv_ppml_se_analytic: float | None = None
    iv_ppml_se_bootstrap: float | None = None
    iv_ppml_range_low: float | None = None
    iv_ppml_range_high: float | None = None
    iv_ppml_bootstrap_reps: int = 0
    iv_ppml_inference: str = ""
    # Half-panel jackknife bias diagnostic, reported and never adopted.
    iv_ppml_spj: float | None = None
    iv_ppml_spj_bias: float | None = None
    iv_ppml_spj_detail: str = ""
    # Rows and units that never reached the fit, by reason.
    iv_ppml_sample_losses: str = ""
    iv_ppml_rows_dropped: int | None = None
    iv_ppml_units_dropped: int | None = None
    # Largest move under an alternative zero-row assumption, and the account
    # of what was varied. Past the configured gap the estimand is demoted.
    iv_ppml_zero_row_gap: float | None = None
    iv_ppml_zero_row_detail: str = ""
    carried_forward_share: float | None = None
    # Share of estimation rows whose price was imputed rather than observed, and
    # the mix of rungs that supplied them.
    imputed_price_share: float | None = None
    price_source_mix: str = ""
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
    # Run 6, Phase 71: the robust set's SHAPE, its hull (not an interval),
    # its total accepted measure and whether its display can be trusted.
    ar_shape: str | None = None
    ar_hull_lo: float | None = None
    ar_hull_hi: float | None = None
    ar_measure: float | None = None
    ar_n_components: int | None = None
    ar_reaches_search_bound: bool | None = None
    ar_display_reliable: bool | None = None
    ar_psd_failures: int | None = None
    ar_detail: str = ""
    # Robust effective first stage (Montiel Olea-Pflueger family).
    effective_first_stage_f: float | None = None
    effective_k: float | None = None
    n_absorbed_parameters: int | None = None
    n_clusters: int | None = None
    # Cluster structure (Run 6, Phase 73, Finding 4). The instruments are
    # ASSIGNED at supplier x month and category x month, so the level at which
    # their variation is correlated - not the level at which rows are many -
    # is what inference has to respect.
    cluster_level_used: str = ""
    cluster_detail: str = ""
    n_clusters_by_dimension: str = ""
    # Clusters weighted by how much identifying variation each carries:
    # the inverse Herfindahl of per-cluster instrument variation. Twelve
    # suppliers of which one moves is not twelve independent clusters.
    effective_clusters: float | None = None
    n_clusters_with_instrument_variation: int | None = None
    max_cluster_variation_share: float | None = None
    cluster_variation_detail: str = ""
    # Small-cluster remedy, when the assignment level leaves too few.
    small_cluster_method_used: str = ""
    small_cluster_detail: str = ""
    primary_interval_basis: str = ""
    analytic_ci_low: float | None = None
    analytic_ci_high: float | None = None
    wild_ci_low: float | None = None
    wild_ci_high: float | None = None
    wild_bootstrap_reps_used: int | None = None
    first_stage_variance_pd: bool | None = None
    effective_f_detail: str = ""
    # Anticipation (lead) test.
    lead_test_status: str = LEAD_UNAVAILABLE
    lead_test_p: float | None = None
    lead_test_horizon: int | None = None
    lead_test_bound: float | None = None
    lead_test_detail: str = ""
    # Per-segment placebo (shuffled instrument), annotated after the fact when
    # the real-data validation suite has run. Informational: it needs the
    # validation run, so it is never a gate - a gate that reads "not evaluated"
    # by default would withdraw every causal status in the default path.
    placebo_status: str = PLACEBO_NOT_RUN
    placebo_share_significant: float | None = None
    placebo_actual_p: float | None = None
    placebo_detail: str = ""
    robustness_spread: float | None = None
    # How many IDENTIFIED specifications the spread covers, and which
    # alternatives were excluded as unidentified (Run 6, Phase 70).
    robustness_n_specifications: int | None = None
    robustness_excluded_weak: str = ""
    identifying_coverage: float | None = None
    # Identification support (24.6, refined in Run 6 Phase 74). The outer
    # bounds are quantiles of the instrument-predicted WITHIN log-price
    # movement - residualized for pair and period fixed effects and for the
    # market control, so they describe movement the instruments induced around
    # a pair's own price path, not the spread of price levels across pairs.
    support_log_lo: float | None = None
    support_log_hi: float | None = None
    # How many estimation rows the support rests on, and where it is actually
    # dense: a proposed move inside the outer bounds but in a gap between two
    # clusters of movement is as much an extrapolation as one beyond them.
    support_n: int | None = None
    support_regions: str = ""
    support_n_regions: int | None = None
    support_window: float | None = None
    support_min_observations: int | None = None
    # The same quantiles of the OBSERVED within price movement, so the report
    # can show how much of the price variation the instruments account for.
    observed_log_lo: float | None = None
    observed_log_hi: float | None = None
    support_detail: str = ""
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


# Which panel columns each configured cluster level uses (Run 6, Phase 73).
# The first four are the levels the INSTRUMENTS are assigned at; the last two
# are outcome-correlation levels kept from Run 4.
CLUSTER_LEVEL_COLUMNS: dict[str, list[str]] = {
    "supplier": ["cluster_supplier"],
    "supplier_month": ["cluster_supplier_month"],
    "category": ["cluster_category"],
    "category_month": ["cluster_category_month"],
    "month": ["cluster_month"],
    "month_customer": ["cluster_month", "cluster_customer"],
}
# Levels at which an instrument is assigned, as opposed to levels chosen for
# outcome correlation. Serial correlation within an assignment unit is the
# failure mode Finding 4 names, so the distinction is reported.
ASSIGNMENT_LEVELS = frozenset({"supplier", "supplier_month", "category", "category_month"})
# How the reported 95% interval was built. Codes, resolved to text by the
# label dictionary wherever a person reads them.
INTERVAL_CLUSTER_ROBUST = "interval_cluster_robust_t"
INTERVAL_WILD_BOOTSTRAP = "interval_wild_cluster_bootstrap"
INTERVAL_WITHDRAWN = "interval_withdrawn_few_clusters"
# Replicates are evaluated in blocks so a long panel costs the same peak
# memory as a short one: this is the number of matrix cells a block may
# hold, which the sample length divides into a replicate count. It is a
# memory dial only - a test pins that changing it cannot move a reported
# number, because the draws are taken in the same order either way.
WILD_BOOTSTRAP_BLOCK_CELLS = 4_000_000
# Plain English for the level, for every string a person reads.
CLUSTER_LEVEL_NAMES: dict[str, str] = {
    "supplier": "supplier",
    "supplier_month": "supplier and month",
    "category": "product category",
    "category_month": "product category and month",
    "month": "month",
    "month_customer": "month and customer",
}


def resolve_cluster_columns(level: str, available: Sequence[str]) -> tuple[list[str], str]:
    """The cluster columns for a configured level, and a note on what happened.

    A panel that predates the full set of keys, or one built without supplier
    or category attributes, simply does not carry the requested column. Rather
    than silently clustering on something else, the missing column is named and
    the level falls back to the coarsest key the panel does have. The resulting
    cluster COUNT then flows into the adequacy gate, so a degenerate fallback
    cannot pass as valid inference.
    """
    want = CLUSTER_LEVEL_COLUMNS.get(level, ["cluster_month"])
    have = [c for c in want if c in available]
    if len(have) == len(want):
        return want, ""
    missing = ", ".join(c for c in want if c not in available)
    for alt in ("cluster_month", "cluster_customer"):
        if alt in available:
            return [alt], (
                f"the panel carries no {missing}, so inference fell back to "
                f"{alt.removeprefix('cluster_')}"
            )
    return [], f"the panel carries no {missing} and no fallback clustering key"


def cluster_variation_profile(
    z: np.ndarray, codes: np.ndarray, n_g: int
) -> tuple[float, int, float]:
    """How the identifying variation is spread over clusters.

    Returns the effective number of clusters, how many carry any instrument
    variation at all, and the largest single share. The effective count is the
    inverse Herfindahl of per-cluster variation shares: with the variation
    spread evenly over G clusters it equals G, and with all of it inside one
    cluster it equals 1. It is the number the adequacy gate reads, because a
    cluster that contributes no instrument movement contributes no information
    about the coefficient however many rows it holds.
    """
    mass = np.zeros(n_g)
    np.add.at(mass, codes, (z * z).sum(axis=1))
    total = float(mass.sum())
    if total <= 0:
        return 0.0, 0, 0.0
    share = mass / total
    eff = float(1.0 / float((share * share).sum()))
    return eff, int((mass > total * 1e-12).sum()), float(share.max())


def _iv_point_and_crv1(
    yz: np.ndarray,
    pz: np.ndarray,
    z: np.ndarray,
    zz_inv: np.ndarray,
    codes: np.ndarray,
    n_g: int,
    dof: float,
) -> tuple[float, float] | None:
    """2SLS coefficient and its one-way CRV1 standard error, for bootstrapping."""
    p_hat = z @ (zz_inv @ (z.T @ pz))
    denom = float(p_hat @ pz)
    if abs(denom) < 1e-12:
        return None
    beta = float((p_hat @ yz) / denom)
    resid = yz - pz * beta
    scores = np.zeros(n_g)
    np.add.at(scores, codes, p_hat * resid)
    var = dof * float(scores @ scores) / (denom * denom)
    if not np.isfinite(var) or var <= 0:
        return None
    return beta, float(np.sqrt(var))


def wild_cluster_bootstrap_iv(
    yz: np.ndarray,
    pz: np.ndarray,
    z: np.ndarray,
    codes: np.ndarray,
    n_g: int,
    *,
    n_absorbed: int,
    reps: int,
    seed: int,
) -> tuple[float, float, int, str] | None:
    """Equal-tailed wild cluster bootstrap-t interval for the 2SLS coefficient.

    This is the wild RESTRICTED EFFICIENT form (Davidson and MacKinnon): both
    equations are resampled with the SAME cluster weight, so the correlation
    between the first-stage and structural errors - the whole reason an
    instrument is needed - survives into the bootstrap world. Bootstrapping the
    structural equation alone, with price held fixed, would quietly assume
    price exogenous inside the very procedure meant to defend an IV estimate.

    Memory is bounded by evaluating replicates in blocks sized to the sample,
    so a long panel costs the same peak as a short one.
    """
    n = yz.shape[0]
    k_total = 1 + n_absorbed
    if n_g <= 1 or n <= k_total:
        return None
    dof = (n_g / (n_g - 1)) * ((n - 1) / (n - k_total))
    zz_inv = np.linalg.pinv(z.T @ z)
    base = _iv_point_and_crv1(yz, pz, z, zz_inv, codes, n_g, dof)
    if base is None:
        return None
    beta0, se0 = base
    pi_hat = zz_inv @ (z.T @ pz)
    fitted_p = z @ pi_hat
    v_hat = pz - fitted_p
    u_hat = yz - pz * beta0

    # Every weight is drawn ONCE, before the blocking, so the block size stays
    # a pure memory dial: drawing per block would advance the generator a
    # different number of times and silently change the reported interval. The
    # draw is one value per cluster per replicate - small, because clusters are
    # few by definition here - while the blocked arrays below are the ones that
    # scale with the sample.
    rng = np.random.default_rng(seed)
    weights = rng.choice(np.array([-1.0, 1.0]), size=(n_g, reps))
    stats_out = np.empty(reps)
    stats_out.fill(np.nan)
    block = max(1, min(reps, int(WILD_BOOTSTRAP_BLOCK_CELLS // max(n, 1))))
    done = 0
    while done < reps:
        b = min(block, reps - done)
        w = weights[:, done : done + b][codes]
        p_star = fitted_p[:, None] + v_hat[:, None] * w
        y_star = p_star * beta0 + u_hat[:, None] * w
        ph = z @ (zz_inv @ (z.T @ p_star))
        den = np.einsum("ij,ij->j", ph, p_star)
        ok = np.abs(den) > 1e-12
        beta_r = np.where(ok, np.einsum("ij,ij->j", ph, y_star) / np.where(ok, den, 1.0), np.nan)
        resid = y_star - p_star * beta_r
        scores = np.zeros((n_g, b))
        np.add.at(scores, codes, ph * resid)
        var = dof * (scores * scores).sum(axis=0) / np.where(ok, den * den, 1.0)
        se_r = np.sqrt(np.where(var > 0, var, np.nan))
        stats_out[done : done + b] = (beta_r - beta0) / se_r
        del w, p_star, y_star, ph, resid, scores
        done += b

    t = stats_out[np.isfinite(stats_out)]
    if t.size < max(99, reps // 2):
        return None
    # SYMMETRIC percentile-t: the 95th percentile of |t*| replaces the
    # normal-theory 1.96. The equal-tailed form (separate 2.5th and 97.5th
    # percentiles) was measured first and was consistently WORSE than the
    # analytic interval it was meant to repair - 88% coverage at ten clusters
    # against the analytic 88%, and 66% against 79% with a weak first stage -
    # because an asymmetric bootstrap distribution shifts the interval off
    # centre faster than it widens it. The symmetric form reached 93% and 88%
    # on the same draws. The measurements are in the build log.
    q = float(np.quantile(np.abs(t), 0.95))
    detail = (
        f"wild cluster bootstrap-t over {n_g} groups, {t.size} of {reps} usable "
        f"replicates; the symmetric 95th percentile of the studentized statistic is "
        f"{q:.2f} against the normal-theory 1.96"
    )
    return beta0 - q * se0, beta0 + q * se0, int(t.size), detail


@dataclass(frozen=True)
class SupportProfile:
    """Where a segment's instruments actually moved price, and how densely."""

    lo: float
    hi: float
    n: int
    regions: tuple[tuple[float, float], ...]
    observed_lo: float
    observed_hi: float
    detail: str

    @property
    def regions_text(self) -> str:
        """Compact, parseable form for the exports: a:b|a:b."""
        return "|".join(f"{a:.6f}:{b:.6f}" for a, b in self.regions)


def identification_support(
    predicted: np.ndarray,
    observed: np.ndarray,
    *,
    lo_quantile: float,
    hi_quantile: float,
    window: float,
    min_observations: int,
    n_bins: int = 60,
) -> SupportProfile:
    """Profile the price movement the instruments induced in one segment.

    `predicted` is the instrument-predicted within log-price movement and
    `observed` the within log-price movement itself, both already residualized
    for the fixed effects and for the market control where one is used. The
    outer bounds are the configured quantiles of `predicted`.

    Inside those bounds the support is not assumed uniform. A proposed move is
    supported only where at least `min_observations` predicted movements sit
    within `window` of it, and the maximal runs of moves meeting that test are
    returned as REGIONS. Skewed support yields one off-centre region; support
    that clusters in two places with a gap between them yields two, and a move
    landing in the gap is refused even though it lies between the outer bounds.
    """
    n = int(predicted.size)
    lo = float(np.quantile(predicted, lo_quantile))
    hi = float(np.quantile(predicted, hi_quantile))
    obs_lo = float(np.quantile(observed, lo_quantile)) if observed.size else lo
    obs_hi = float(np.quantile(observed, hi_quantile)) if observed.size else hi
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return SupportProfile(
            lo, hi, n, (), obs_lo, obs_hi, "the instruments induced no usable price movement"
        )
    # Evaluate the "enough movements nearby" test on a grid across the bounds.
    # Sorting once makes every window count a pair of binary searches.
    grid = np.linspace(lo, hi, n_bins + 1)
    order = np.sort(predicted)
    left = np.searchsorted(order, grid - window, side="left")
    right = np.searchsorted(order, grid + window, side="right")
    dense = (right - left) >= min_observations
    regions: list[tuple[float, float]] = []
    start: float | None = None
    for i, ok in enumerate(dense):
        if ok and start is None:
            start = float(grid[i])
        elif not ok and start is not None:
            regions.append((start, float(grid[i - 1])))
            start = None
    if start is not None:
        regions.append((start, float(grid[-1])))
    # A single grid point is an artefact of the grid, not a region of support.
    regions = [(a, b) for a, b in regions if b > a]
    if not regions:
        detail = (
            f"no part of the {lo:+.1%} to {hi:+.1%} range carries "
            f"{min_observations} instrument-induced movements within "
            f"{window:.1%} of it, over {n} rows"
        )
    else:
        spans = ", ".join(f"{a:+.1%} to {b:+.1%}" for a, b in regions)
        gap = (
            ""
            if len(regions) == 1
            else f", in {len(regions)} separate stretches with gaps between them"
        )
        detail = (
            f"instrument-induced price movement spans {lo:+.1%} to {hi:+.1%} over {n} rows; "
            f"moves are supported where at least {min_observations} of those movements sit "
            f"within {window:.1%}, which holds for {spans}{gap}. Observed price movement "
            f"over the same rows spans {obs_lo:+.1%} to {obs_hi:+.1%}"
        )
    return SupportProfile(lo, hi, n, tuple(regions), obs_lo, obs_hi, detail)


def support_distance(move: float, regions: Sequence[tuple[float, float]]) -> float:
    """Zero when the move sits in a supported region, otherwise the log-price
    distance to the nearest one. Positive numbers say how far the proposal
    reaches beyond anything the instruments induced."""
    if not regions:
        return float("inf")
    return min(max(a - move, move - b, 0.0) for a, b in regions)


def parse_support_regions(text: str) -> tuple[tuple[float, float], ...]:
    """Read back the compact export form written by `SupportProfile`."""
    if not text:
        return ()
    out: list[tuple[float, float]] = []
    for part in text.split("|"):
        a, _, b = part.partition(":")
        out.append((float(a), float(b)))
    return tuple(out)


def price_source_mix(work: pl.DataFrame) -> str:
    """Where the prices on a segment's estimation rows came from, largest
    share first.

    The NAME breaks ties on the count. `group_by` output order is not
    guaranteed, so sorting on the count alone leaves two equal counts in
    whichever order the hash table happened to produce - which makes this
    string, and the parquet export carrying it, differ between identical runs.
    The scenario matrix caught it on a world where observed and
    carried-forward prices each take 49% of the rows, about six runs in ten.
    """
    if not work.height:
        return ""
    rows = (
        work.group_by("price_status")
        .agg(pl.len().alias("n"))
        .sort(["n", "price_status"], descending=[True, False])
        .to_dicts()
    )
    return "; ".join(
        f"{r['price_status'].replace('_', ' ')} {r['n'] / work.height:.0%}" for r in rows
    )


def _spec_min_groups(spec: ClusterSpec) -> int:
    """Degrees of freedom driver: the smallest positive-sign group count."""
    return min(n_g for _, n_g, sign in spec if sign > 0)


def _cluster_meat(x: np.ndarray, resid: np.ndarray, codes: np.ndarray, n_g: int) -> np.ndarray:
    """Sum over clusters of (X_g' u_g)(X_g' u_g)', vectorized."""
    k = x.shape[1]
    scores = np.zeros((n_g, k))
    np.add.at(scores, codes, x * resid[:, None])
    return scores.T @ scores


def _cluster_ols(
    y: np.ndarray, x: np.ndarray, spec: ClusterSpec, n_absorbed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """OLS beta and CRV1 cluster-robust vcov (no intercept; demeaned inputs).
    Multiway vcov combines per-dimension sandwiches with CGM signs; a
    non-positive-definite combination falls back to the largest one-way
    component (standard CGM practice for the scalar case).

    `n_absorbed` (Run 6, Phase 71) counts the parameters the WITHIN
    transformation already removed - pair and time fixed effects, plus a
    partialled control. They are invisible in `x` but were estimated from the
    same rows, so leaving them out of the degrees-of-freedom factor overstates
    precision in exactly the thin segments where weak identification bites.
    """
    xtx = x.T @ x
    xtx_inv = np.linalg.pinv(xtx)
    beta = xtx_inv @ (x.T @ y)
    resid = y - x @ beta
    n, k = x.shape
    k_total = k + n_absorbed
    if n <= k_total:
        # The design is saturated: the within transformation plus the regressors
        # have used up every observation, so there are no residual degrees of
        # freedom and the residuals are driven to the rounding floor. The old
        # code set the small-sample factor to 1.0 here and returned a variance
        # of 1e-12 to 1e-24, which `pinv` inverts without complaint and which
        # turns any coefficient into an enormous Wald statistic. There is no
        # variance to report, so it is reported as infinite and every gate that
        # reads it fails closed.
        return beta, np.full((k, k), np.inf)
    parts: list[np.ndarray] = []
    for codes, n_g, sign in spec:
        meat = _cluster_meat(x, resid, codes, n_g)
        dof = (n_g / (n_g - 1)) * ((n - 1) / (n - k_total)) if n_g > 1 else 1.0
        parts.append(sign * dof * xtx_inv @ meat @ xtx_inv)
    vcov = np.sum(np.stack(parts), axis=0)
    # A Cameron-Gelbach-Miller combination subtracts one sandwich from another
    # and can land with a non-positive variance on any diagonal entry, not only
    # in the scalar case. The previous guard was reachable only at k == 1, so a
    # multi-regressor design (the anticipation test runs at k = 5 or 6) could
    # report a negative or zero variance as precision. Fall back to the largest
    # positive one-way component per entry; when no component is positive the
    # variance is not estimable and is reported as infinite, which fails the
    # strength and precision gates instead of passing them on a variance of
    # zero.
    if len(parts) > 1:
        bad = [i for i in range(k) if not (float(vcov[i, i]) > 0)]
        for i in bad:
            positives = [float(p[i, i]) for p in parts if float(p[i, i]) > 0]
            vcov[i, i] = max(positives) if positives else np.inf
    elif k >= 1 and any(not (float(vcov[i, i]) > 0) for i in range(k)):
        for i in range(k):
            if not (float(vcov[i, i]) > 0):
                vcov[i, i] = np.inf
    return beta, vcov


def _first_stage_f(p: np.ndarray, z: np.ndarray, spec: ClusterSpec, n_absorbed: int = 0) -> float:
    """Cluster-robust Wald divided by the number of instruments. This is the
    long-standing HEURISTIC statistic compared against 10; it is not a
    classical first-stage F and has no derived critical value for a clustered
    variance structure. The gate uses the robust effective F instead."""
    beta, vcov = _cluster_ols(p, z, spec, n_absorbed)
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
) -> tuple[list[float], int]:
    """Anderson-Rubin confidence set by grid inversion: betas where instruments
    have no explanatory power for y - beta*p (cluster-robust Wald, multiway
    via CGM signs).

    Vectorized over the grid: the reduced-form coefficient and per-cluster
    scores are linear in beta, so meat(beta) is a quadratic matrix form.
    """
    k = z.shape[1]
    # Small-cluster critical value (Run 6, Phase 73). A cluster-robust Wald
    # statistic is only chi-square in the number of clusters, not the number
    # of rows, and this test is inverted over a grid at every segment - so with
    # twelve suppliers the chi-square bar (5.99 at two instruments) accepts too
    # little and reports a set narrower than the evidence. K x F(K, G-K) is the
    # standard small-sample form and converges back to chi-square as clusters
    # grow: at 288 clusters it is 6.05 against 5.99, at 24 it is 6.89, at 12 it
    # is 8.21. The rest of this estimator already uses t and F critical values
    # for the same reason.
    from scipy import stats as _st

    n_g_min = _spec_min_groups(spec)
    crit = float(k * _st.f.ppf(0.95, k, max(n_g_min - k, 1)))
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
    psd_failures = 0
    for b in grid:
        # Wald-form AR: reduced-form coefficient of (y - b p) on Z with
        # cluster-robust vcov; both are affine/quadratic in b.
        coef = a0 - b * a1
        meat = sum(d * (m00 - b * (m01 + m01.T) + b * b * m11) for d, m00, m01, m11 in terms)
        vcov = ztz_inv @ meat @ ztz_inv
        try:
            w = float(coef @ np.linalg.pinv(vcov) @ coef)
        except np.linalg.LinAlgError:
            # Skipping a point is equivalent to REJECTING it, which narrows
            # the reported set; the count is returned so the caller can mark
            # the display unreliable rather than print a clean range.
            psd_failures += 1
            continue
        if not np.isfinite(w):
            psd_failures += 1
            continue
        if w <= crit:
            accepted.append(float(b))
    return accepted, psd_failures


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
    with_total_quantity: bool = True,
) -> SegmentEstimate:
    """`market_control_source` is the public-source decision from
    `select_market_index` (Run 6, Phase 70) and `market_control_grain` the
    index's join grain (e.g. "item_category:month"). Passing the default means
    no control is in evidence, which caps the status - never a silent pass.

    `with_total_quantity` controls whether the second estimand (Run 6, Phase 72)
    is fitted. It defaults to on, so a caller never silently loses it; the two
    internal loops that pool or backtest the CONDITIONAL estimate pass False,
    because they never read the total-quantity number and fitting a second
    Poisson per window or per child segment is work whose result is discarded.
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
    if "price_status" in work.columns and work.height:
        # The carried-forward share alone stopped being the disclosure it was
        # once the price ladder existed (Run 6, Phase 72): with a price list in
        # the data almost nothing reaches that rung, so the figure went to zero
        # while a fifth of the panel still carried an IMPUTED price. What a
        # reader needs to know is how much of the panel is imputed at all, and
        # by which rung.
        # IMPUTED means the price was constructed, not observed. A lost quote is
        # a real price the customer was given for a month they did not buy, so
        # it is reported in the mix but is not counted as an imputation.
        est.imputed_price_share = float(
            work["price_status"].is_in(["carried_forward", "list_x_pair_discount"]).mean() or 0.0  # type: ignore[arg-type]
        )
        est.price_source_mix = price_source_mix(work)
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

    cluster_cols, cluster_note = resolve_cluster_columns(sc.cluster_level, pos.columns)
    est.cluster_level_used = sc.cluster_level
    est.cluster_detail = cluster_note
    if not cluster_cols:
        est.status_reasons.append(
            f"no grouping key for {CLUSTER_LEVEL_NAMES.get(sc.cluster_level, sc.cluster_level)}: "
            f"{cluster_note}"
        )
        return est
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
    # Robustness against imputed zero-row prices (24.8): the same PPML on
    # positive-quantity rows only; the gap joins the robustness criterion.
    est.ppml_positive = _ppml(pos, rules)

    # --- total-quantity estimand (Run 6, Phase 72) ---------------------------
    # Reported beside the conditional estimate, never above it. The ladder has
    # no causal tier at all, so nothing downstream can price from this number.
    if with_total_quantity and sc.iv_ppml_enabled:
        _total_quantity_estimate(est, work, rules, time_fe, cluster_cols, use_control=has_mkt)

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

    # Parameters the within transformation already removed, plus the
    # partialled control: they were estimated from these rows, so the
    # degrees-of-freedom factor must count them (Run 6, Phase 71).
    n_absorbed = (
        int(pdf["pair"].nunique()) + int(pdf[time_fe].nunique()) + (1 if use_control else 0)
    )
    est.n_absorbed_parameters = n_absorbed
    est.n_clusters = _spec_min_groups(spec)
    # Cluster structure report (Run 6, Phase 73). The binding dimension is the
    # one with fewest groups: it drives the degrees-of-freedom factor and the
    # critical value, so it is the one the variation profile is taken over.
    bind_codes, bind_n = min(((c, n) for c, n, sign in spec if sign > 0), key=lambda cn: cn[1])
    est.n_clusters_by_dimension = "; ".join(
        f"{col} {n}"
        for col, (_c, n, _s) in zip(cluster_cols, spec[: len(cluster_cols)], strict=False)
    )
    eff_g, n_varying, max_share = cluster_variation_profile(z, bind_codes, bind_n)
    est.effective_clusters = eff_g
    est.n_clusters_with_instrument_variation = n_varying
    est.max_cluster_variation_share = max_share
    level_kind = (
        "the level the instruments are assigned at"
        if sc.cluster_level in ASSIGNMENT_LEVELS
        else "an outcome-correlation level, not an instrument-assignment level"
    )
    est.cluster_variation_detail = (
        f"grouped by {CLUSTER_LEVEL_NAMES.get(sc.cluster_level, sc.cluster_level)} "
        f"({level_kind}): "
        f"{bind_n} groups, {n_varying} carrying any instrument movement, "
        f"largest single share {max_share:.0%}, effective count {eff_g:.1f}"
    )
    est.first_stage_f = _first_stage_f(pz, z, spec, n_absorbed)
    # Robust effective first stage: the statistic whose critical value is
    # derived for an arbitrary (clustered) variance structure. This is what
    # the gate uses; the Wald/K heuristic above stays reported.
    _, vcov_pi = _cluster_ols(pz, z, spec, n_absorbed)
    eff = effective_first_stage_f(pz, z, vcov_pi, n_absorbed=n_absorbed, n_clusters=est.n_clusters)
    est.effective_first_stage_f = eff.f_eff
    est.effective_k = eff.k_eff
    est.first_stage_variance_pd = eff.variance_positive_definite
    est.effective_f_detail = eff.detail
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
    # Identification support (24.6, refined in Run 6 Phase 74). `p_hat` is the
    # instrument-predicted WITHIN log-price movement: pair and period fixed
    # effects, and the market control where one is used, have already been
    # taken out of both sides, so this is movement the instruments induced
    # around each pair's own price path.
    prof = identification_support(
        p_hat,
        pz,
        lo_quantile=sc.price_support_lo_quantile,
        hi_quantile=sc.price_support_hi_quantile,
        window=sc.price_support_window,
        min_observations=sc.min_support_observations,
    )
    est.support_log_lo = prof.lo
    est.support_log_hi = prof.hi
    est.support_n = prof.n
    est.support_regions = prof.regions_text
    est.support_n_regions = len(prof.regions)
    est.support_window = sc.price_support_window
    est.support_min_observations = sc.min_support_observations
    est.observed_log_lo = prof.observed_lo
    est.observed_log_hi = prof.observed_hi
    est.support_detail = prof.detail

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
        est.analytic_ci_low = beta_iv - crit * est.iv_se
        est.analytic_ci_high = beta_iv + crit * est.iv_se
        est.iv_ci_low, est.iv_ci_high = est.analytic_ci_low, est.analytic_ci_high
        est.primary_interval_basis = INTERVAL_CLUSTER_ROBUST

    # --- small-cluster remedy (Run 6, Phase 73) ------------------------------
    # Clustering where the instrument is ASSIGNED is the correct level and the
    # expensive one: a world with twelve suppliers has twelve clusters however
    # many rows it has, and the cluster-robust t interval is then materially
    # too narrow. The count that matters is the EFFECTIVE one - clusters
    # weighted by the identifying variation they carry - because a cluster
    # whose instrument never moves tells the coefficient nothing.
    cluster_fail_closed = False
    eff_count = est.effective_clusters
    few = eff_count is not None and eff_count < sc.min_assignment_clusters
    if not few:
        est.small_cluster_method_used = "not needed"
        est.small_cluster_detail = (
            f"effective group count {eff_count:.1f} is at or above the "
            f"{sc.min_assignment_clusters} the standard interval needs"
            if eff_count is not None
            else "the spread of identifying movement across groups could not be measured"
        )
    else:
        shortfall = (
            f"effective group count {eff_count:.1f} (of {est.n_clusters} groups) is below the "
            f"{sc.min_assignment_clusters} the standard interval needs"
        )
        if len(cluster_cols) > 1:
            # The wild cluster bootstrap resamples whole clusters, which needs a
            # partition. A two-way specification is an intersection of two
            # overlapping partitions and has no such thing, so there is no
            # bootstrap to fall back to here - only the honest refusal.
            cluster_fail_closed = True
            est.small_cluster_method_used = "interval withdrawn"
            est.small_cluster_detail = (
                f"{shortfall}, and a two-way grouping has no single partition to "
                "resample, so no correction for having few groups applies"
            )
        elif est.n_clusters is not None and est.n_clusters <= sc.min_bootstrap_clusters:
            cluster_fail_closed = True
            est.small_cluster_method_used = "interval withdrawn"
            est.small_cluster_detail = (
                f"{shortfall}, and at {est.n_clusters} groups resampling is not a "
                f"remedy either: below {sc.min_bootstrap_clusters} groups it was "
                "measured to add noise rather than improve on the standard interval"
            )
        elif sc.small_cluster_method == "fail_closed":
            cluster_fail_closed = True
            est.small_cluster_method_used = "interval withdrawn"
            est.small_cluster_detail = f"{shortfall}; configured policy is to fail closed"
        else:
            boot = wild_cluster_bootstrap_iv(
                yz,
                pz,
                z,
                bind_codes,
                bind_n,
                n_absorbed=n_absorbed,
                reps=sc.wild_bootstrap_reps,
                seed=2_000_003 + 7919 * segment,
            )
            if boot is None:
                cluster_fail_closed = True
                est.small_cluster_method_used = "interval withdrawn"
                est.small_cluster_detail = (
                    f"{shortfall}, and the wild cluster bootstrap did not produce enough "
                    "usable replicates to replace the analytic interval"
                )
            else:
                lo, hi, used, detail = boot
                est.wild_ci_low, est.wild_ci_high = lo, hi
                est.wild_bootstrap_reps_used = used
                est.iv_ci_low, est.iv_ci_high = lo, hi
                est.primary_interval_basis = INTERVAL_WILD_BOOTSTRAP
                est.small_cluster_method_used = "resampled interval"
                est.small_cluster_detail = f"{shortfall}, so the reported interval is a {detail}"
    if cluster_fail_closed:
        # The point estimate survives; its INTERVAL does not, and every
        # downstream consumer reads the interval. Withdrawing it is the whole
        # point of failing closed, so it is withdrawn rather than shown with a
        # caveat. The analytic figures stay on the record under their own names.
        est.iv_ci_low = est.iv_ci_high = None
        est.primary_interval_basis = INTERVAL_WITHDRAWN

    # Anderson-Rubin set, described as the SHAPE it is (Run 6, Phase 71).
    # On contact with the search edge the grid is extended outward ONCE at a
    # coarser step, so the report can say whether the set is bounded anywhere
    # economically plausible rather than only that it touched -5.
    step = 0.02
    grid_lo, grid_hi = -5.0, 1.0
    grid = np.arange(grid_lo, grid_hi, step)
    accepted, psd_failures = _ar_confidence_set(yz, pz, z, spec, grid)
    if accepted and (
        min(accepted) <= grid_lo + step * 1.01 or max(accepted) >= grid_hi - step * 1.01
    ):
        wide_step = 0.1
        wide_lo, wide_hi = -15.0, 5.0
        wide = np.arange(wide_lo, wide_hi, wide_step)
        wide_accepted, wide_psd = _ar_confidence_set(yz, pz, z, spec, wide)
        if wide_accepted:
            accepted = sorted(set(accepted) | set(wide_accepted))
            psd_failures += wide_psd
            grid_lo, grid_hi, step = wide_lo, wide_hi, wide_step
    ar = ar_set_from_accepted(
        accepted, grid_lo=grid_lo, grid_hi=grid_hi, step=step, psd_failures=psd_failures
    )
    est.ar_shape = ar.shape
    est.ar_hull_lo, est.ar_hull_hi = ar.hull_lo, ar.hull_hi
    # Deprecated aliases: the extent of the accepted set is a HULL, not a
    # confidence interval. Kept so older bundles and parity tests still read.
    est.ar_ci_low, est.ar_ci_high = ar.hull_lo, ar.hull_hi
    est.ar_measure = ar.measure
    est.ar_n_components = len(ar.components)
    est.ar_reaches_search_bound = ar.reaches_lower_bound or ar.reaches_upper_bound
    est.ar_display_reliable = ar.display_reliable
    est.ar_detail = ar.detail
    est.ar_psd_failures = psd_failures
    # "Bounded" now means: a connected range that does not run to the search
    # edge, is not degenerate, and whose display is reliable.
    est.ar_bounded = ar.shape == AR_SHAPE_INTERVAL and ar.display_reliable and not ar.degenerate

    # Anticipation (lead) test (Run 6, Phase 71): FUTURE instrument values
    # must not predict current quantity once current and lagged values are
    # controlled. Current and lagged z stay in the design because the
    # instrument processes are AR(1) - without them a lead block would pick up
    # persistence rather than anticipation.
    est.lead_test_horizon = sc.lead_test_horizon
    lead = _lead_test(pdf, z_cols, time_fe, cluster_cols, spec, rules, use_control=use_control)
    est.lead_test_status = lead.status
    est.lead_test_p = lead.p_value
    est.lead_test_bound = lead.detectable_bound
    est.lead_test_detail = lead.detail

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

    # The GATE uses the robust effective F (Montiel Olea-Pflueger family),
    # whose critical value is derived for an arbitrary clustered variance
    # structure (Run 6, Phase 71). The long-standing Wald/K statistic is kept
    # REPORTED next to it: it is a heuristic with no derived critical value
    # here, so it informs but does not decide.
    f_eff = est.effective_first_stage_f
    f_ok = f_eff is not None and f_eff >= sc.min_effective_first_stage_f
    heur = (
        f"; reported heuristic Wald/K {est.first_stage_f:.1f} against 10"
        if est.first_stage_f is not None
        else ""
    )
    gate(
        3,
        "first_stage_strength",
        f_ok,
        (
            f"robust effective F {f_eff:.1f} against the published "
            f"{sc.min_effective_first_stage_f} critical value, over "
            f"{est.n_clusters} independent clusters after absorbing "
            f"{est.n_absorbed_parameters} fixed-effect parameters{heur}"
            if f_eff is not None
            else f"{est.effective_f_detail}{heur}"
        ),
    )
    if not f_ok:
        f_txt = f"{f_eff:.1f}" if f_eff is not None else "not computable"
        reasons.append(
            f"robust effective first-stage F {f_txt} below {sc.min_effective_first_stage_f}"
        )
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons = reasons
        est.causal_status_reasons = gates
        est.estimand, est.estimand_bias_direction = estimand_for(
            iv_ran=False, path_resolved=path.resolved
        )
        return est
    # Cluster structure (Run 6, Phase 73, Finding 4). Clustering at the level
    # the instruments are ASSIGNED at is what makes the interval honest; doing
    # so can leave few clusters, and this gate is where that cost is paid in
    # the open rather than absorbed into an interval that looks precise.
    gate(
        4,
        "cluster_structure_adequate",
        not cluster_fail_closed,
        f"{est.cluster_variation_detail}. {est.small_cluster_detail}",
    )
    if cluster_fail_closed:
        reasons.append(f"cluster structure: {est.small_cluster_detail}")
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons = reasons
        est.causal_status_reasons = gates
        est.estimand, est.estimand_bias_direction = estimand_for(
            iv_ran=False, path_resolved=path.resolved
        )
        return est
    overid_ok = est.overid_p is None or est.overid_p >= sc.overid_alpha
    gate(
        5,
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
            6,
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
            6,
            "robust_across_specifications",
            spread_ok,
            f"estimates span {est.robustness_spread:.2f} across "
            f"{est.robustness_n_specifications} identified specifications against a "
            f"{sc.robustness_max_spread} maximum" + excluded_txt,
        )
        if not spread_ok:
            reasons.append(f"robustness spread {est.robustness_spread:.2f} exceeds maximum")
    # Imputed-zero-row-price robustness (24.8): the PPML gap between the full
    # panel, whose zero rows carry an IMPUTED price, and positive rows only,
    # whose prices are all observed. The gate used to call this the
    # "carried-forward" comparison, which stopped being true once the price
    # ladder existed (Run 6, Phase 72): with a price list in the data the zero
    # rows are priced at list times the pair's trailing discount, and almost
    # none are carried forward. The gate now names the rungs actually present.
    cf_gap = (
        abs(est.ppml - est.ppml_positive)
        if est.ppml is not None and est.ppml_positive is not None
        else None
    )
    mix_txt = f" Zero-row prices: {est.price_source_mix}." if est.price_source_mix else ""
    gate(
        7,
        "imputed_price_robustness",
        None if cf_gap is None else cf_gap <= sc.robustness_max_spread,
        f"the PPML estimate moves {cf_gap:.2f} between the full panel, whose zero rows "
        f"carry imputed prices, and positive rows only, against a "
        f"{sc.robustness_max_spread} maximum; note PPML is fitted WITHOUT the "
        f"market control, so this compares a controlled headline to uncontrolled fits." + mix_txt
        if cf_gap is not None
        else "one of the two PPML fits did not converge, so the gap is unknown",
    )
    if cf_gap is not None and cf_gap > sc.robustness_max_spread:
        reasons.append(
            f"imputed zero-row price robustness gap {cf_gap:.2f} exceeds maximum "
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
        8,
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
    ar_detail = est.ar_detail or "the weak-instrument-robust set was not computed"
    ar_ok = bool(est.ar_bounded)
    if ar_ok and ci_width is not None and est.ar_measure is not None:
        # Compare the set's total accepted MEASURE (grid step included) with
        # the primary interval, so a one-point set cannot read as width zero.
        ar_ok = est.ar_measure <= sc.ar_max_width_ratio * ci_width
        if not ar_ok:
            ar_detail = (
                f"the weak-instrument-robust set accepts {est.ar_measure:.2f} in total, "
                f"beyond {sc.ar_max_width_ratio}x the primary interval ({ci_width:.2f})"
            )
    gate(9, "weak_instrument_robust_agreement", ar_ok, ar_detail)
    if not ar_ok:
        reasons.append(f"weak-instrument-robust set: {ar_detail}")
    # An EMPTY robust set with two instruments is the cluster-robust
    # overidentification test rejecting: the model is refused everywhere, so
    # the status drops to descriptive rather than merely losing the top rung.
    if est.ar_shape == AR_SHAPE_EMPTY:
        est.status = STATUS_DESCRIPTIVE
        est.status_reasons = reasons
        est.causal_status_reasons = gates
        est.estimand, est.estimand_bias_direction = estimand_for(
            iv_ran=False, path_resolved=path.resolved
        )
        return est

    # Anticipation: a REJECTION is direct evidence of an exclusion violation
    # that the overidentification test cannot see, so it blocks. An
    # inconclusive or unavailable result is reported as exactly that and does
    # NOT block - the spec asks for the test to be added and reported, and a
    # non-rejection of a low-power test is not evidence of exogeneity either
    # way. The detectable bound is shown so nobody reads silence as proof.
    # The criterion is a MATERIAL anticipation effect. A detected but immaterial
    # one passes it, with the finding named in the detail: the engine's own
    # materiality threshold is what decides, on both branches of the test. An
    # inconclusive result is recorded as NOT EVALUATED rather than as a failure -
    # the same situation as an overidentification test with one instrument, where
    # the check could not speak. Recording it as a failure while the status stays
    # causal would make the gate record contradict the status it explains.
    gate(
        10,
        "no_material_anticipation_effect",
        None
        if est.lead_test_status in (LEAD_UNAVAILABLE, LEAD_INCONCLUSIVE)
        else est.lead_test_status in (LEAD_PASSED, LEAD_IMMATERIAL),
        est.lead_test_detail or "the anticipation test was not run",
    )
    if est.lead_test_status == LEAD_REJECTED:
        reasons.append(f"anticipation detected: {est.lead_test_detail}")

    est.status = STATUS_CAUSAL if not reasons else STATUS_IV
    est.status_reasons = reasons
    est.causal_status_reasons = gates
    est.estimand, est.estimand_bias_direction = estimand_for(
        iv_ran=True, path_resolved=path.resolved
    )
    return est


def _lead_test(
    pdf: pd.DataFrame,
    z_cols: list[str],
    time_fe: str,
    cluster_cols: list[str],
    spec: ClusterSpec,
    rules: Rules,
    *,
    use_control: bool,
) -> LeadTest:
    """Regress within-transformed log quantity on current, lagged and LEAD
    instrument values and test the lead block jointly.

    Inference is F(K*H, G-1): with a few dozen clusters a chi-square bar
    over-rejects, and the rest of this estimator already uses t/F critical
    values for the same reason.
    """
    sc = rules.sensitivity
    h = sc.lead_test_horizon
    df = pdf.copy()
    df = df.sort_values(["pair", "month_idx"])
    # ONE aggregate lead regressor rather than K*H separate ones. With a few
    # dozen clusters a 4-restriction cluster-robust Wald over-rejects badly,
    # and the individual leads are nearly collinear with the current and
    # lagged values anyway (the instrument processes are AR(1)). Testing the
    # combined future movement keeps one restriction, which the small-cluster
    # F critical value can actually support.
    lead_parts: list[str] = []
    keep = ["log_qty", "pair", time_fe, *cluster_cols]
    # Leads and lags are taken by CALENDAR month, not by row position: the panel
    # is sparse, so a positional shift would silently reach past a gap and call
    # a value from three months ahead "next month". One merge per offset carries
    # every instrument at once - merging on (pair, month) is the expensive step
    # here, so a two-instrument horizon-2 design does 3 merges, not 6.
    for offset in (*range(1, h + 1), -1):
        names = {c: (f"_{c}_lead{offset}" if offset > 0 else f"_{c}_lag1") for c in z_cols}
        src = df[["pair", "month_idx", *z_cols]].rename(columns=names)
        src["month_idx"] = src["month_idx"] - offset
        df = df.merge(src, on=["pair", "month_idx"], how="left")
        if offset > 0:
            lead_parts.extend(names[c] for c in z_cols)
    for c in z_cols:
        keep += [c, f"_{c}_lag1"]
    # ONE aggregate regressor, but a scale-free one: the instruments are
    # different series with different variances, so a raw sum would weight
    # whichever happens to be larger. Each lead is divided by its own standard
    # deviation first, which makes the aggregate a direction rather than an
    # arbitrary mixture.
    scaled = []
    for name in lead_parts:
        sd = float(df[name].std())
        scaled.append(df[name] / sd if np.isfinite(sd) and sd > 0 else df[name] * 0.0)
    df["_z_lead_sum"] = sum(scaled[1:], scaled[0]) if scaled else 0.0
    lead_names: list[str] = ["_z_lead_sum"]
    if use_control and "log_mkt" in df.columns:
        keep.append("log_mkt")
    cols = [*dict.fromkeys([*keep, *lead_names, *lead_parts])]
    n_positive = len(pdf)
    df = df[cols].dropna()
    n_rows = len(df)
    # The guard counts RESIDUAL degrees of freedom, not rows. Requiring a row
    # count alone let saturated designs through: the pair and time fixed effects
    # can absorb more parameters than there are rows, at which point the
    # residuals collapse to the rounding floor and any coefficient becomes an
    # enormous Wald statistic. Measured on thin segments, that produced
    # rejection rates of 8-30% with no anticipation present at all.
    n_reg = sum(
        1
        for c in cols
        if c not in ("log_qty", "pair", time_fe, *cluster_cols) and c not in lead_parts
    )
    n_absorbed_est = int(df["pair"].nunique()) + int(df[time_fe].nunique()) if n_rows else 0
    residual_dof = n_rows - n_reg - n_absorbed_est
    if (
        n_rows < max(sc.min_observations, 50)
        or df["pair"].nunique() < 5
        or residual_dof < sc.lead_test_min_residual_dof
    ):
        return lead_test_from_moments(
            wald=None,
            coefficients=None,
            std_errors=None,
            dof_num=len(lead_names),
            dof_den=0,
            horizon=h,
            n_rows=n_rows,
            alpha=sc.lead_test_alpha,
            meaningful_effect=sc.lead_test_meaningful_effect,
            n_positive_rows=n_positive,
            residual_dof=residual_dof,
        )
    # The sample lost its first and last periods, so the cluster spec is
    # rebuilt on the rows that remain.
    lead_spec = _make_cluster_spec(*(df[c].to_numpy() for c in cluster_cols))
    reg_cols = [
        c
        for c in cols
        if c not in ("log_qty", "pair", time_fe, *cluster_cols) and c not in lead_parts
    ]
    dm = _two_way_demean(df, ["log_qty", *reg_cols], "pair", time_fe)
    y = dm["log_qty"].to_numpy()
    x = dm[reg_cols].to_numpy()
    beta, vcov = _cluster_ols(y, x, lead_spec, n_absorbed_est)
    idx = [reg_cols.index(n) for n in lead_names]
    sub_v = vcov[np.ix_(idx, idx)]
    sub_b = beta[idx]
    wald: float | None
    try:
        wald = float(sub_b @ np.linalg.pinv(sub_v) @ sub_b)
    except np.linalg.LinAlgError:
        wald = None
    diag = np.diag(sub_v)
    ses = np.sqrt(np.where(diag > 0, diag, np.nan))
    if wald is None or not np.isfinite(wald) or not np.all(np.isfinite(ses)):
        return lead_test_from_moments(
            wald=None,
            coefficients=None,
            std_errors=None,
            dof_num=len(lead_names),
            dof_den=max(_spec_min_groups(lead_spec) - 1, 0),
            horizon=h,
            n_rows=n_rows,
            alpha=sc.lead_test_alpha,
            meaningful_effect=sc.lead_test_meaningful_effect,
            n_positive_rows=n_positive,
            residual_dof=residual_dof,
        )
    return lead_test_from_moments(
        wald=wald,
        coefficients=sub_b,
        std_errors=ses,
        dof_num=len(lead_names),
        dof_den=max(_spec_min_groups(lead_spec) - 1, 0),
        horizon=h,
        n_rows=n_rows,
        alpha=sc.lead_test_alpha,
        meaningful_effect=sc.lead_test_meaningful_effect,
        n_positive_rows=n_positive,
        residual_dof=residual_dof,
    )


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


@dataclass(frozen=True)
class IvPpmlFit:
    """An IV-Poisson fit on the panel INCLUDING zeros, via a control function.

    The estimand is total expected quantity - whether the customer buys at all,
    and how much - rather than the quantity conditional on buying. That is the
    quantity an opportunity forecast needs, and the conditional 2SLS estimate
    cannot supply it.

    The price of including the zeros is a stronger assumption than 2SLS makes:
    the first-stage error must enter the exponential index linearly and
    additively separably from the price. 2SLS needs only a linear projection.
    Nothing in the data tests it, so it is stated wherever the estimate is
    reported rather than left implicit.
    """

    beta: float | None
    rho: float | None  # control-function coefficient; informative only under H0
    rho_t: float | None
    n_rows: int
    n_positive: int
    n_pairs: int
    first_stage_f: float | None
    detail: str
    # Analytic cluster-robust standard error on the price coefficient. Free -
    # the Poisson fit computes it anyway - but it treats the control function as
    # a known regressor when it was estimated, so it OMITS first-stage sampling
    # noise and is a lower bound. Labeled as such wherever it appears.
    se_analytic: float | None = None
    # Pair cluster bootstrap (Run 6, Phase 72): refits the whole procedure,
    # first stage included, on pairs resampled with replacement, so it DOES
    # carry the generated-regressor noise the analytic figure omits.
    se_bootstrap: float | None = None
    range_low: float | None = None
    range_high: float | None = None
    bootstrap_reps: int = 0
    inference: str = ""
    # Split-panel (half-panel) jackknife for the incidental-parameter bias that
    # Kwon, Larch, Yoon & Yotov (2026, CESifo WP 12641) document for IV-PPML
    # with high-dimensional fixed effects: fit the two time halves, and the
    # bias-corrected point is 2 x full - mean(halves).
    spj: float | None = None
    spj_bias: float | None = None
    spj_detail: str = ""
    # Rows and units that never reached the fit, by reason.
    sample_losses: str = ""
    rows_dropped: int | None = None
    units_dropped: int | None = None


def _iv_ppml_sample_losses(
    panel_seg: pl.DataFrame, z_cols: list[str], time_fe: str
) -> tuple[str, int, int]:
    """Account for every row and unit the IV-Poisson fit does not see.

    Rows AND units, because the two tell different stories: a thousand dropped
    zero rows cost no volume and can still remove the whole extensive-margin
    signal, while one dropped key-value pair can be a tenth of the segment's
    quantity. Each reason is counted on what survived the previous one, so the
    figures add to the total instead of overlapping.

    Computed here rather than read back from the estimator, so the accounting
    does not depend on a particular pyfixest version's reporting and exists even
    when the fit itself fails.
    """
    total_rows = panel_seg.height
    if not total_rows:
        return "the segment has no rows", 0, 0
    total_units = int(panel_seg["qty"].sum() or 0)
    steps: list[tuple[str, int, int]] = []
    df = panel_seg

    def drop(reason: str, keep: pl.Expr) -> None:
        nonlocal df
        mask = keep.fill_null(value=False)
        gone = df.filter(~mask)
        if gone.height:
            steps.append((reason, gone.height, int(gone["qty"].sum() or 0)))
        df = df.filter(mask)

    if "contract_id" in df.columns:
        drop("priced on a contract, which is excluded by design", pl.col("contract_id").is_null())
    drop("no price", pl.col("log_price").is_not_null())
    for c in z_cols:
        drop(f"no {c.replace('_', ' ')} instrument value", pl.col(c).is_not_null())

    # Singleton fixed effects, dropped iteratively exactly as the estimator
    # drops them: removing a singleton pair can leave a period with one row.
    before, units_before = df.height, (int(df["qty"].sum() or 0) if df.height else 0)
    while df.height:
        sized = df.with_columns(
            pl.len().over("pair").alias("_np"), pl.len().over(time_fe).alias("_nt")
        )
        kept = sized.filter((pl.col("_np") > 1) & (pl.col("_nt") > 1)).drop("_np", "_nt")
        if kept.height == df.height:
            break
        df = kept
    if df.height < before:
        steps.append(
            (
                "absorbed by a fixed effect with a single observation",
                before - df.height,
                units_before - (int(df["qty"].sum() or 0) if df.height else 0),
            )
        )

    # Separation: a pair that never buys carries no information for a Poisson
    # with pair fixed effects - its fixed effect runs to minus infinity and the
    # estimator drops it. Those are exactly the customers who stopped buying, so
    # losing them silently would hollow out the estimand this phase exists for.
    if df.height:
        before, units_before = df.height, int(df["qty"].sum() or 0)
        # Iteratively, over BOTH fixed effects and until stable, because the
        # cascade is real: dropping a pair that never buys can leave a period
        # in which nothing is bought, which separates in turn.
        while df.height:
            kept = df.filter(
                (pl.col("qty").max().over("pair") > 0).fill_null(value=False)
                & (pl.col("qty").max().over(time_fe) > 0).fill_null(value=False)
            )
            if kept.height == df.height:
                break
            df = kept
        if df.height < before:
            steps.append(
                (
                    "in a pair that never buys, which a Poisson with pair fixed effects "
                    "cannot use (separation)",
                    before - df.height,
                    units_before - (int(df["qty"].sum() or 0) if df.height else 0),
                )
            )

    rows_dropped = total_rows - df.height
    units_dropped = total_units - (int(df["qty"].sum() or 0) if df.height else 0)
    if not steps:
        return f"every one of the {total_rows:,} segment rows reached the fit", 0, 0
    parts = [
        f"{r:,} rows ({u:,} units) {reason}" for reason, r, u in sorted(steps, key=lambda s: -s[1])
    ]
    text = (
        f"{rows_dropped:,} of {total_rows:,} rows and {units_dropped:,} of {total_units:,} "
        "units did not reach the fit: " + "; ".join(parts)
    )
    return text, rows_dropped, units_dropped


def _first_stage_cf(
    pdf: pd.DataFrame,
    z_cols: list[str],
    time_fe: str,
    spec: ClusterSpec,
    *,
    use_control: bool,
    observed_price: np.ndarray | None = None,
) -> tuple[np.ndarray | None, float | None, int]:
    """Full-panel first stage, returning the residual the control function uses.

    Same construction as the conditional first stage - two-way within
    transformation, then the market control partialled out by Frisch-Waugh -
    applied to every panel row, so the residual is defined for the zero rows the
    Poisson stage needs.

    The COEFFICIENT, however, is estimated only on rows whose price was actually
    observed. A zero row's price is imputed from the pair's last purchase, so it
    carries a stale cost shock: with AR(1) instruments at 0.70 and a mean
    staleness near two months, measured pass-through on zero rows is 0.31
    against 0.72 on purchase rows. Fitting the coefficient on all rows pulled it
    from 0.72 to 0.62, and because a zero row contributes nothing to the
    quantity side, only the first stage shrank - inflating the instrumented
    price coefficient by one over that factor, always away from zero. Measured
    on the well-identified preset, that was a median absolute error of 0.31
    against a planted truth the conditional estimator recovers to 0.03. The
    first stage asks how the instruments move the price a customer actually
    faced, and an imputed price is not an observation of that.
    """
    cols = ["log_price", *z_cols] + (["log_mkt"] if use_control and "log_mkt" in pdf else [])
    dm = _two_way_demean(pdf, cols, "pair", time_fe)
    pz = dm["log_price"].to_numpy()
    z = np.column_stack([dm[c].to_numpy() for c in z_cols])
    if use_control and "log_mkt" in pdf:
        mkt = dm["log_mkt"].to_numpy()
        pz = _resid(pz, mkt)
        z = np.column_stack([_resid(z[:, j], mkt) for j in range(z.shape[1])])
    del dm
    n_absorbed = int(pdf["pair"].nunique()) + int(pdf[time_fe].nunique())
    if observed_price is None:
        observed_price = np.ones(len(pz), dtype=bool)
    if observed_price.sum() < max(len(z_cols) + 1, 30):
        return None, None, n_absorbed
    pz_fit, z_fit = pz[observed_price], z[observed_price]
    # Strength is reported for the rows the coefficient is fitted on, since that
    # is the variation doing the identifying.
    # Subsetting cluster codes must RE-INDEX them densely: the codes are labels
    # into an array sized by the group count, so carrying the original labels
    # into a smaller sample indexes past the end of it.
    spec_fit: ClusterSpec = []
    for codes, _n, sign in spec:
        sub, n_sub = _cluster_codes(codes[observed_price])
        spec_fit.append((sub, n_sub, sign))
    f = _first_stage_f(pz_fit, z_fit, spec_fit, n_absorbed)
    try:
        gamma = np.linalg.solve(z_fit.T @ z_fit, z_fit.T @ pz_fit)
    except np.linalg.LinAlgError:
        return None, f, n_absorbed
    return pz - z @ gamma, f, n_absorbed


def _iv_ppml_point(
    pdf: pd.DataFrame,
    z_cols: list[str],
    time_fe: str,
    cluster_cols: list[str],
    *,
    use_control: bool,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """One complete IV-Poisson pass: first stage, control function, Poisson.

    Returns (beta, beta's analytic clustered SE, rho, rho's t, first-stage F).
    Shared by the point estimate, every bootstrap replicate and each
    split-panel half, so a replicate reruns the first stage exactly as the point
    estimate did - which is the whole reason a resampled interval carries
    generated-regressor noise and the analytic one cannot.

    Frames are built and released inside the call. A replicate that kept its
    pyfixest fit alive would hold that replicate's design matrix for as long as
    the caller held the result, and the caller holds hundreds.
    """
    import pyfixest as pf

    # The first-stage strength keeps the same (possibly two-way) cluster spec
    # the conditional estimator uses; the Poisson's own variance clusters
    # one-way on the first column, which is what pyfixest accepts.
    cluster_col = cluster_cols[0]
    spec = _make_cluster_spec(*(pdf[c].to_numpy() for c in cluster_cols))
    observed = (
        (pdf["price_status"].to_numpy() == "observed")
        if "price_status" in pdf.columns
        else (pdf["qty"].to_numpy() > 0)
    )
    v, f, _absorbed = _first_stage_cf(
        pdf, z_cols, time_fe, spec, use_control=use_control, observed_price=observed
    )
    del spec
    if v is None:
        return None, None, None, None, None
    use_mkt = use_control and "log_mkt" in pdf.columns
    rhs = ["log_price", "_cf_v"] + (["log_mkt"] if use_mkt else [])
    wanted = ["qty", "pair", time_fe, cluster_col, *rhs]
    fit_df = pdf.loc[
        :, [c for c in dict.fromkeys(wanted) if c != "_cf_v" and c in pdf.columns]
    ].copy()
    fit_df["_cf_v"] = v
    formula = f"qty ~ {' + '.join(rhs)} | pair + {time_fe}"
    # The clustered variance matters here: on this data an iid standard error
    # makes the control-function coefficient significant almost always, which
    # would read as evidence of endogeneity when it is an artifact.
    fit = None
    try:
        fit = pf.fepois(formula, data=fit_df, vcov={"CRV1": cluster_col})
        beta = float(fit.coef()["log_price"])
        beta_se = float(fit.se()["log_price"])
        rho = float(fit.coef()["_cf_v"])
        rho_se = float(fit.se()["_cf_v"])
    except Exception:
        return None, None, None, None, f
    finally:
        # pyfixest keeps the design matrix on the fit object; dropping both here
        # bounds a bootstrap's live memory to one replicate rather than B.
        del fit, fit_df
    rho_t = rho / rho_se if rho_se > 0 else None
    return beta, (beta_se if beta_se > 0 else None), rho, rho_t, f


def _iv_ppml_bootstrap(
    pdf: pd.DataFrame,
    z_cols: list[str],
    time_fe: str,
    cluster_cols: list[str],
    *,
    use_control: bool,
    reps: int,
    seed: int,
) -> tuple[float | None, float | None, float | None, int, str]:
    """Pair cluster bootstrap: resample pairs with replacement and refit.

    The pair is the resampling unit because it is the panel's cross-sectional
    unit and because drawing it whole preserves the within-pair serial
    correlation the fixed effects do not remove. Each drawn copy of a pair gets
    a FRESH pair id, so a pair drawn twice contributes two fixed effects rather
    than one doubled group - without that, the design a replicate fits is not
    the design the point estimate fitted.

    What it carries and what it misses, both published with the number: it
    carries first-stage sampling noise, which the analytic clustered figure
    omits by treating the control function as known. It does NOT carry
    dependence common to pairs within a period at the level the instrument is
    assigned, because resampling that level would duplicate periods and break
    the time fixed effects. Where the two differ, the wider one is the safer
    read, and both are reported.

    Memory is bounded to one replicate: each builds its own frame from numpy
    takes and releases it before the next, so B replicates cost one replicate's
    peak rather than B times it.
    """
    pairs = pdf["pair"].to_numpy()
    uniq, inverse = np.unique(pairs, return_inverse=True)
    n_pairs = len(uniq)
    if n_pairs < 20:
        return None, None, None, 0, f"no resampled interval: {n_pairs} pairs is too few to resample"
    order = np.argsort(inverse, kind="stable")
    starts = np.searchsorted(inverse[order], np.arange(n_pairs))
    sizes = np.diff(np.append(starts, len(order)))
    cols = [c for c in pdf.columns if c != "pair"]
    base = {c: pdf[c].to_numpy() for c in cols}
    rng = np.random.default_rng(seed)
    draws: list[float] = []
    failures = 0
    for _ in range(reps):
        picks = rng.integers(0, n_pairs, n_pairs)
        idx = np.concatenate([order[starts[p] : starts[p] + sizes[p]] for p in picks])
        rep = pd.DataFrame({c: base[c][idx] for c in cols})
        rep["pair"] = np.repeat(np.arange(n_pairs), sizes[picks])
        beta, _se, _rho, _rt, _f = _iv_ppml_point(
            rep, z_cols, time_fe, cluster_cols, use_control=use_control
        )
        del rep, idx
        if beta is None or not np.isfinite(beta):
            failures += 1
            continue
        draws.append(float(beta))
    if len(draws) < max(10, reps // 2):
        return (
            None,
            None,
            None,
            len(draws),
            f"no resampled interval: only {len(draws)} of {reps} replicates converged",
        )
    arr = np.asarray(draws)
    se = float(arr.std(ddof=1))
    lo, hi = (float(x) for x in np.quantile(arr, [0.025, 0.975]))
    note = (
        f"pair cluster bootstrap, {len(draws)} of {reps} replicates converged, first stage "
        f"refitted in each. It carries first-stage sampling noise, which the analytic "
        f"clustered figure omits; it does not carry dependence common to pairs within a period"
    )
    if failures:
        note += f"; {failures} replicates did not converge and were dropped"
    return se, lo, hi, len(draws), note


def _iv_ppml_split_panel(
    pdf: pd.DataFrame,
    z_cols: list[str],
    time_fe: str,
    cluster_cols: list[str],
    *,
    use_control: bool,
    beta_full: float,
) -> tuple[float | None, float | None, str]:
    """Half-panel jackknife for the incidental-parameter bias (Dhaene-Jochmans).

    Kwon, Larch, Yoon & Yotov (2026, CESifo WP 12641) show that IV-PPML with
    high-dimensional fixed effects inherits an incidental-parameter bias of
    order 1/T and that a split-panel jackknife removes its leading term. This
    panel is 24 months with pair fixed effects, which is the regime that bias
    lives in, so the correction is computed rather than assumed away.

    It is REPORTED, not adopted as the point estimate. The correction doubles
    the full-panel estimate and subtracts the mean of two half-panel fits, so it
    roughly triples the variance of whatever it is applied to; on a ladder whose
    ceiling is already the instrumented tier, trading a bias of the measured
    size for that much variance is not an improvement. The measured comparison
    is in docs/methodology.md. Where the correction is large, the estimate
    itself is the thing in doubt, and the figure says so.
    """
    periods = np.sort(pdf[time_fe].unique())
    if len(periods) < 6:
        return None, None, f"no split-panel correction: {len(periods)} periods is too few to halve"
    cut = periods[len(periods) // 2]
    halves: list[float] = []
    for mask in (pdf[time_fe] < cut, pdf[time_fe] >= cut):
        half = pdf.loc[mask]
        beta, _se, _rho, _rt, _f = _iv_ppml_point(
            half, z_cols, time_fe, cluster_cols, use_control=use_control
        )
        del half
        if beta is None or not np.isfinite(beta):
            return None, None, "no split-panel correction: a half-panel fit did not converge"
        halves.append(float(beta))
    corrected = 2.0 * beta_full - float(np.mean(halves))
    bias = beta_full - corrected
    return (
        corrected,
        bias,
        (
            f"half-panel jackknife over {len(periods)} periods: halves {halves[0]:.3f} and "
            f"{halves[1]:.3f}, bias-corrected point {corrected:.3f}, implied bias {bias:+.3f}. "
            f"Reported as a diagnostic, not adopted: the correction roughly triples the "
            f"estimate's variance"
        ),
    )


def _iv_ppml_fit(
    panel_seg: pl.DataFrame,
    rules: Rules,
    z_cols: list[str],
    time_fe: str,
    cluster_cols: list[str],
    *,
    use_control: bool,
    segment: int = 0,
    diagnostics: bool = True,
) -> IvPpmlFit:
    """FE-Poisson on the full panel with the first-stage residual included.

    `diagnostics` off gives the point estimate alone. The zero-row robustness
    refits pass False: they ask only whether the number moves, and paying for a
    bootstrap and a split-panel correction on a refit nobody publishes would
    triple the cost of the check for no added information.

    The residual enters as a regressor (the control function), which is what
    makes the price coefficient an instrumented one. Its own coefficient is
    reported but never published with an interval: the generated-regressor
    correction vanishes only under the null that it is zero, so the statistic
    is informative about that null and about nothing else.
    """
    try:
        import pyfixest  # noqa: F401
    except Exception as exc:  # pragma: no cover - import guard
        return IvPpmlFit(None, None, None, 0, 0, 0, None, f"pyfixest unavailable: {exc}")

    sc = rules.sensitivity
    # Skipped on a refit nobody publishes: the robustness variants want the
    # coefficient, not a second accounting of rows they deliberately removed.
    losses, rows_dropped, units_dropped = (
        _iv_ppml_sample_losses(panel_seg, z_cols, time_fe) if diagnostics else ("", None, None)
    )

    keep = ["qty", "log_price", "pair", "month_idx", *z_cols, *cluster_cols]
    if "price_status" in panel_seg.columns:
        keep.append("price_status")
    if "availability_buyers" in panel_seg.columns:
        keep.append("availability_buyers")
    if time_fe != "month_idx":
        keep.append(time_fe)
    if use_control and "log_mkt" in panel_seg.columns:
        keep.append("log_mkt")
    df = panel_seg.filter(
        pl.col("contract_id").is_null() & pl.col("log_price").is_not_null()
    ).select([c for c in dict.fromkeys(keep) if c in panel_seg.columns])
    df = df.drop_nulls(subset=["qty", "log_price", *z_cols])
    pdf = _to_pandas(df)
    del df
    n_rows = len(pdf)
    n_pos = int((pdf["qty"] > 0).sum()) if n_rows else 0
    n_pairs = int(pdf["pair"].nunique()) if n_rows else 0

    def short(detail: str, f: float | None = None) -> IvPpmlFit:
        return IvPpmlFit(
            None,
            None,
            None,
            n_rows,
            n_pos,
            n_pairs,
            f,
            detail,
            sample_losses=losses,
            rows_dropped=rows_dropped,
            units_dropped=units_dropped,
        )

    if n_rows < max(sc.min_observations, 50) or n_pos == 0:
        return short("too few panel rows, or no positive quantity, for an IV-Poisson fit")

    cluster_col = cluster_cols[0]
    beta, se_analytic, rho, rho_t, f = _iv_ppml_point(
        pdf, z_cols, time_fe, cluster_cols, use_control=use_control
    )
    if beta is None:
        return short(
            "the full-panel first stage could not be solved, or the IV-Poisson fit did not "
            "converge, so no total-quantity estimate exists",
            f,
        )

    inference = (
        f"analytic standard error {se_analytic:.3f}, cluster-robust at "
        f"{cluster_col.replace('_', ' ')}; it treats the control function as a known regressor "
        f"when it was estimated, so it omits first-stage sampling noise"
        if se_analytic is not None
        else "the analytic standard error could not be computed"
    )
    se_boot: float | None = None
    lo: float | None = None
    hi: float | None = None
    reps_done = 0
    if diagnostics and sc.iv_ppml_bootstrap_reps > 0:
        se_boot, lo, hi, reps_done, note = _iv_ppml_bootstrap(
            pdf,
            z_cols,
            time_fe,
            cluster_cols,
            use_control=use_control,
            reps=sc.iv_ppml_bootstrap_reps,
            # Seeded from the segment so rerunning the same world reproduces the
            # same interval; an interval that wanders between runs is not a
            # measurement.
            seed=1_000_003 + 7919 * segment,
        )
        inference += ". " + note
    else:
        inference += (
            ". No resampled interval was computed in this run: the pair cluster bootstrap "
            "refits the whole procedure once per replicate, which the measured pipeline budget "
            "does not carry, so it is switched on by configuration "
            "(sensitivity.iv_ppml_bootstrap_reps) for the validation studies"
        )
    spj: float | None = None
    spj_bias: float | None = None
    spj_detail = "the split-panel correction is switched off in this run"
    if diagnostics and sc.iv_ppml_split_panel_jackknife:
        spj, spj_bias, spj_detail = _iv_ppml_split_panel(
            pdf, z_cols, time_fe, cluster_cols, use_control=use_control, beta_full=beta
        )
    del pdf
    return IvPpmlFit(
        beta=beta,
        rho=rho,
        rho_t=rho_t,
        n_rows=n_rows,
        n_positive=n_pos,
        n_pairs=n_pairs,
        first_stage_f=f,
        detail=(
            f"IV-Poisson on {n_rows:,} panel rows ({n_pos:,} with a purchase) across "
            f"{n_pairs:,} pairs, clustered at {cluster_col.replace('_', ' ')}"
        ),
        se_analytic=se_analytic,
        se_bootstrap=se_boot,
        range_low=lo,
        range_high=hi,
        bootstrap_reps=reps_done,
        inference=inference,
        spj=spj,
        spj_bias=spj_bias,
        spj_detail=spj_detail,
        sample_losses=losses,
        rows_dropped=rows_dropped,
        units_dropped=units_dropped,
    )


def _zero_row_robustness(
    work: pl.DataFrame,
    rules: Rules,
    z_cols: list[str],
    time_fe: str,
    cluster_cols: list[str],
    *,
    use_control: bool,
    beta: float,
) -> tuple[float | None, str]:
    """Refit the total-quantity estimate under alternative zero-row assumptions.

    Zero rows are the whole reason this estimand exists, and two of the things
    the engine believes about them are inferences rather than observations: the
    price the customer would have faced, and whether the item was available at
    all. Neither can be checked directly, so each is tested by removing the
    rows that depend on it most and asking whether the answer changes.

    * **Price.** Drop the zero rows carrying an imputed price, keeping purchases
      and any no-purchase month with a real quoted price. If the estimate moves,
      the imputation was carrying it.
    * **Availability.** Drop the zero rows whose availability rests on a single
      other customer having bought the item that month - the weakest form the
      proxy takes. If the estimate moves, the proxy was carrying it.

    Returns the larger of the two gaps and a plain-English account. The gap
    demotes the estimate to descriptive past the configured threshold: an
    estimate that depends on which guess was made about the zero rows is not an
    instrumented measurement of anything.
    """
    sc = rules.sensitivity
    variants: list[tuple[str, pl.DataFrame]] = []
    if "price_status" in work.columns:
        variants.append(
            (
                "dropping no-purchase months priced by imputation",
                work.filter(
                    (pl.col("qty") > 0) | pl.col("price_status").is_in(["observed", "quoted"])
                ),
            )
        )
    if "availability_buyers" in work.columns:
        variants.append(
            (
                "dropping no-purchase months whose availability rests on a single buyer",
                work.filter((pl.col("qty") > 0) | (pl.col("availability_buyers") > 1)),
            )
        )
    gaps: list[tuple[float, str]] = []
    skipped: list[str] = []
    for label, alt in variants:
        if alt.height == work.height:
            skipped.append(f"{label} removed no rows")
            continue
        fit = _iv_ppml_fit(
            alt,
            rules,
            z_cols,
            time_fe,
            cluster_cols,
            use_control=use_control,
            diagnostics=False,
        )
        if fit.beta is None:
            skipped.append(f"{label} left too little to fit")
            continue
        gaps.append((abs(fit.beta - beta), f"{label}: {fit.beta:.2f} against {beta:.2f}"))
    if not gaps:
        return None, (
            "the zero-row assumptions could not be varied ("
            + "; ".join(skipped or ["no alternative was available"])
            + ")"
        )
    worst = max(gaps, key=lambda g: g[0])
    detail = "; ".join(g[1] for g in gaps)
    verdict = (
        f"the estimate moves {worst[0]:.2f} at most when the zero-row assumptions are varied, "
        f"against a {sc.iv_ppml_max_zero_row_gap} maximum ({detail})"
    )
    if skipped:
        verdict += "; " + "; ".join(skipped)
    return worst[0], verdict


def _total_quantity_estimate(
    est: SegmentEstimate,
    work: pl.DataFrame,
    rules: Rules,
    time_fe: str,
    cluster_cols: list[str],
    *,
    use_control: bool,
) -> None:
    """Fit the total-quantity estimand and record it with an honest ceiling.

    The ladder stops at the instrumented tier by construction. Three things are
    missing that the conditional ladder has, and each is named on the record
    rather than left for a reader to discover: the measured accuracy on planted
    worlds is roughly six times worse, no overidentification test exists for
    this estimand (the linear J is computed on positive rows and tests the
    conditional model), and the temporal backtest predicts positive-quantity
    rows, so this estimate has no out-of-sample check.
    """
    sc = rules.sensitivity
    z_cols: list[str] = [c for c in ("z_cost", "z_tariff") if c in work.columns]
    if not z_cols:
        est.iv_ppml_status = STATUS_TOTAL_INSUFFICIENT
        est.iv_ppml_reasons = "no instruments in the data, so total quantity is not instrumented"
        return
    fit = _iv_ppml_fit(
        work, rules, z_cols, time_fe, cluster_cols, use_control=use_control, segment=est.segment
    )
    est.iv_ppml = fit.beta
    est.iv_ppml_first_stage_f = fit.first_stage_f
    est.iv_ppml_rho_t = fit.rho_t
    est.iv_ppml_n_rows = fit.n_rows
    est.iv_ppml_detail = fit.detail
    est.iv_ppml_se_analytic = fit.se_analytic
    est.iv_ppml_se_bootstrap = fit.se_bootstrap
    est.iv_ppml_range_low = fit.range_low
    est.iv_ppml_range_high = fit.range_high
    est.iv_ppml_bootstrap_reps = fit.bootstrap_reps
    est.iv_ppml_inference = fit.inference
    est.iv_ppml_spj = fit.spj
    est.iv_ppml_spj_bias = fit.spj_bias
    est.iv_ppml_spj_detail = fit.spj_detail
    # The sample-loss accounting is recorded even when the fit fails, because
    # the reason it failed is usually in it.
    est.iv_ppml_sample_losses = fit.sample_losses
    est.iv_ppml_rows_dropped = fit.rows_dropped
    est.iv_ppml_units_dropped = fit.units_dropped
    if fit.beta is None:
        est.iv_ppml_status = STATUS_TOTAL_INSUFFICIENT
        est.iv_ppml_reasons = fit.detail
        return
    # The CHEAP disqualifiers run first, and the expensive ones only on a
    # segment they left standing. Both orders give the same statuses - the
    # robustness check can only demote, and a segment already demoted cannot be
    # demoted further - so this is pure saved work: two extra Poisson fits per
    # segment that were being spent to re-reject segments the first stage had
    # already rejected. Measured at about 0.37s per segment on a 400k-row panel.
    reasons: list[str] = []
    if fit.first_stage_f is None or fit.first_stage_f < sc.min_first_stage_f:
        reasons.append(
            "the first stage on observed prices is too weak to instrument total quantity"
        )
    if (
        est.imputed_price_share is not None
        and est.imputed_price_share > sc.iv_ppml_max_imputed_share
    ):
        reasons.append(
            f"{est.imputed_price_share:.0%} of the panel carries an imputed price, above the "
            f"{sc.iv_ppml_max_imputed_share:.0%} this estimand is reported at"
        )
    if not sc.iv_ppml_zero_row_robustness:
        est.iv_ppml_zero_row_detail = "the zero-row robustness check is switched off in this run"
    elif reasons:
        # Already demoted on a cheap check, so the refits cannot change the
        # status. Say that, rather than leaving an empty field that would read
        # as "varied, and nothing moved".
        est.iv_ppml_zero_row_detail = (
            "the zero-row assumptions were not varied: this segment is already reported as "
            "descriptive on an earlier check, which the refits could only confirm"
        )
    else:
        gap, detail = _zero_row_robustness(
            work, rules, z_cols, time_fe, cluster_cols, use_control=use_control, beta=fit.beta
        )
        est.iv_ppml_zero_row_gap = gap
        est.iv_ppml_zero_row_detail = detail
        if gap is not None and gap > sc.iv_ppml_max_zero_row_gap:
            reasons.append(
                f"the estimate moves {gap:.2f} when the zero-row price and availability "
                f"assumptions are varied, above the {sc.iv_ppml_max_zero_row_gap} this "
                f"estimand is reported at"
            )
        elif gap is None:
            reasons.append("the zero-row assumptions could not be varied, so none was tested")
    est.iv_ppml_status = STATUS_TOTAL_DESCRIPTIVE if reasons else STATUS_TOTAL_IV
    # The ceiling reason is permanent, not conditional: it applies even when
    # every check above passes.
    reasons.append(
        "the total-quantity ladder has no causal tier: on planted worlds with a real "
        "extensive margin this estimator recovers the total elasticity to a median "
        "absolute error of 0.15 against the 0.02 the conditional estimator achieves on "
        "its own estimand, it has no overidentification test, and the temporal backtest "
        "does not cover it"
    )
    est.iv_ppml_reasons = "; ".join(reasons)


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
    with_total_quantity: bool = True,
) -> SensitivityResult:
    """Estimate every segment; returns the estimates and the gate record.

    `with_total_quantity` is on by default, so a run never silently loses the
    second estimand (Run 6, Phase 72). Simulation studies that measure the
    sampling behaviour of the CONDITIONAL estimator - interval coverage,
    bootstrap stability - pass False: they never read the total-quantity
    number, and fitting a second Poisson per simulation per segment is work
    whose result is thrown away. Measured: with the second estimand fitted in
    the loop, the 50-simulation coverage study alone took 3,518s - longer than
    the entire suite had previously taken.
    """
    segs = sorted(s for s in panel["segment_leaf"].unique().to_list() if s is not None)

    def one(s: int) -> SegmentEstimate:
        seg_panel = panel.filter(pl.col("segment_leaf") == s)
        return estimate_segment(
            seg_panel,
            rules,
            int(s),
            market_control_source=market_control_source,
            market_control_grain=market_control_grain,
            with_total_quantity=with_total_quantity,
        )

    # Segments are independent, so they can run concurrently. Threads rather
    # than processes: the work sits in numpy and pyfixest, which release the
    # GIL, and on Windows a process pool would pay a fresh interpreter import
    # per worker and copy each segment's frame through a pickle - both larger
    # than the fit being parallelized. Memory is the binding constraint, so the
    # worker count is configuration, not `cpu_count()`: each concurrent segment
    # holds its own filtered frame.
    #
    # ORDER IS RESTORED BEFORE ANYTHING IS BUILT. Estimates are collected into
    # a dict keyed by segment and read back in sorted order, so the exports are
    # byte-identical to the sequential path whatever order the pool finishes in
    # - which the determinism tests check.
    workers = max(1, min(rules.sensitivity.max_parallel_segments, len(segs)))
    if workers > 1 and len(segs) > 1:
        from concurrent.futures import ThreadPoolExecutor

        # Longest first. Segments are wildly uneven - 21k to 154k rows on the
        # demo world - so the finishing time is set by the largest one, and
        # starting it last leaves it running alone after everything else is
        # done. Submitting in descending size order is the standard
        # longest-processing-time schedule and costs one group_by to compute.
        sizes = dict(
            panel.group_by("segment_leaf")
            .agg(pl.len().alias("n"))
            .select("segment_leaf", "n")
            .iter_rows()
        )
        order = sorted(segs, key=lambda s: -int(sizes.get(s, 0)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = dict(zip(order, pool.map(one, order), strict=True))
        ests = [done[s] for s in segs]
    else:
        ests = [one(s) for s in segs]

    rows = [est.to_dict() for est in ests]
    gate_rows: list[dict[str, Any]] = []
    for est in ests:
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
    with_total_quantity: bool = True,
) -> pl.DataFrame:
    """Estimates only (the long-standing signature)."""
    return run_sensitivity_full(
        panel,
        rules,
        market_control_source=market_control_source,
        market_control_grain=market_control_grain,
        with_total_quantity=with_total_quantity,
    ).estimates


def annotate_placebo(estimates: pl.DataFrame, placebo: pl.DataFrame) -> pl.DataFrame:
    """Attach the shuffled-instrument placebo verdict to each segment estimate.

    The placebo suite runs after estimation, so this is an annotation rather
    than part of the gate record. Segments with no placebo row keep
    `placebo_not_run` and say so.
    """
    from margin_engine.sensitivity.real_validation import PLACEBO_NOMINAL_ALPHA

    rows = {
        int(r["segment"]): r
        for r in placebo.filter(pl.col("test") == "shuffled_instrument").to_dicts()
        if r.get("segment") is not None
    }
    statuses: list[str] = []
    shares: list[float | None] = []
    actuals: list[float | None] = []
    details: list[str] = []
    for seg in estimates["segment"].to_list():
        row = rows.get(int(seg)) if seg is not None else None
        share = row.get("share_significant") if row else None
        actual = row.get("actual") if row else None
        draws = row.get("n_draws") if row else None
        status, detail = placebo_verdict(
            float(share) if share is not None else None,
            float(actual) if actual is not None else None,
            n_draws=int(draws) if draws is not None else 0,
            alpha=PLACEBO_NOMINAL_ALPHA,
        )
        statuses.append(status)
        shares.append(float(share) if share is not None else None)
        actuals.append(float(actual) if actual is not None else None)
        details.append(detail)
    return estimates.with_columns(
        pl.Series("placebo_status", statuses, dtype=pl.String),
        pl.Series("placebo_share_significant", shares, dtype=pl.Float64),
        pl.Series("placebo_actual_p", actuals, dtype=pl.Float64),
        pl.Series("placebo_detail", details, dtype=pl.String),
    )
