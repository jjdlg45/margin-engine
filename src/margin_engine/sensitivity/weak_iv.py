"""Weak-instrument diagnostics (Run 6, Phase 71 / Findings 4, 13).

Three things live here, kept out of the estimator so each can be reasoned
about and tested on its own:

1. **Effective first-stage F** (Montiel Olea-Pflueger family), the statistic
   whose critical value is derived for an ARBITRARY variance structure:

       F_eff = (pi' Z'Z pi) / trace(V_hat Z'Z)

   with pi the first-stage coefficients and V_hat the cluster-robust variance
   of pi. Two corrections that matter, both of which bias the statistic
   TOWARD passing if skipped:
   - the degrees-of-freedom factor must count the parameters actually
     absorbed (pair and time fixed effects, plus the partialled market
     control), not just the visible regressors. The within transformation
     removes them before the estimator sees the data, so the naive factor
     over-states precision in exactly the thin segments where weak
     identification matters.
   - a Cameron-Gelbach-Miller combination can fail to be positive definite.
     Checking only `trace <= 0` lets a broken variance through with a large,
     meaningless F_eff, so the smallest eigenvalue is checked instead.

   The classical heuristic (cluster-robust Wald / K against 10) stays
   REPORTED, and is explicitly not the same statistic: it has no derived
   critical value for this variance structure.

2. **Anderson-Rubin set shape.** The accepted grid points are grouped into
   CONNECTED COMPONENTS, so a set that is empty, a single point, two
   disjoint ranges, or running to the edge of the search range is reported as
   that - never collapsed into a symmetric interval. The reported extent is a
   HULL, named as one, not a confidence interval. Each component carries the
   grid step, so a one-point set has positive measure rather than reading as
   "nothing is accepted".

3. **Lead (anticipation) test.** Future instrument values should not predict
   current quantity once current and lagged values are controlled. Inference
   uses F(K*H, G-1) with G the smallest cluster count, matching the rest of
   the estimator's small-cluster treatment - a chi-square bar would
   over-reject with ~24 month clusters. Three outcomes, because a
   non-rejection of a low-power test is NOT evidence of exogeneity:
   rejected, inconclusive (the interval cannot exclude an economically
   meaningful anticipation effect) and passed. The magnitude, interval and
   the smallest effect the test could have detected are all reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import stats

# Montiel Olea-Pflueger simplified critical value for the effective F at the
# 10% worst-case-bias threshold, 5% level. This is the one DERIVED bar in this
# module; everything else is a transplanted heuristic and is labeled so.
MOP_CRITICAL_VALUE_10PCT_BIAS = 23.109

AR_SHAPE_EMPTY = "ar_set_empty"
AR_SHAPE_POINT = "ar_set_single_point"
AR_SHAPE_INTERVAL = "ar_set_interval"
AR_SHAPE_DISCONNECTED = "ar_set_disconnected"
AR_SHAPE_REACHES_BOUND = "ar_set_reaches_search_bound"

LEAD_PASSED = "lead_test_passed"
LEAD_REJECTED = "lead_test_rejected"
LEAD_INCONCLUSIVE = "lead_test_inconclusive"
LEAD_IMMATERIAL = "lead_test_immaterial"
LEAD_UNAVAILABLE = "lead_test_unavailable"


@dataclass(frozen=True)
class EffectiveF:
    """The robust first-stage statistic and what went into it."""

    f_eff: float | None
    k_eff: float | None  # effective number of instruments
    n_absorbed: int
    n_clusters: int
    variance_positive_definite: bool
    detail: str


def effective_first_stage_f(
    price_within: np.ndarray,
    z_within: np.ndarray,
    vcov_pi: np.ndarray,
    *,
    n_absorbed: int,
    n_clusters: int,
) -> EffectiveF:
    """F_eff = pi' Z'Z pi / trace(V_hat Z'Z), with V_hat already carrying the
    corrected degrees-of-freedom factor from the caller.

    `n_absorbed` is the number of parameters removed by the within
    transformation and any partialled control; it is reported so a reader can
    see how thin the segment really was.
    """
    zz = z_within.T @ z_within
    if not np.all(np.isfinite(vcov_pi)) or not np.all(np.isfinite(zz)):
        return EffectiveF(
            f_eff=None,
            k_eff=None,
            n_absorbed=n_absorbed,
            n_clusters=n_clusters,
            variance_positive_definite=False,
            detail="the clustered first-stage variance is not estimable",
        )
    sym = (vcov_pi + vcov_pi.T) / 2.0
    try:
        eigs = np.linalg.eigvalsh(sym)
        min_eig = float(eigs.min())
        max_eig = float(np.abs(eigs).max())
    except np.linalg.LinAlgError:
        min_eig, max_eig = -1.0, 1.0
    # Indefiniteness is judged RELATIVE to the matrix's own scale. A
    # Cameron-Gelbach-Miller combination subtracts one sandwich from another and
    # can land genuinely indefinite, which would leave a positive trace and
    # therefore a large, meaningless F_eff; that is refused. An eigenvalue at
    # the rounding floor of a positive-semidefinite sandwich (within-transformed
    # log prices are numerically tiny) is arithmetic noise, not a broken
    # variance, and refusing it would silently withhold a usable statistic.
    noise_floor = max(max_eig, 1.0) * 1e4 * float(np.finfo(float).eps)
    if min_eig < -noise_floor:
        return EffectiveF(
            f_eff=None,
            k_eff=None,
            n_absorbed=n_absorbed,
            n_clusters=n_clusters,
            variance_positive_definite=False,
            detail=(
                "the clustered first-stage variance is not positive definite "
                f"(smallest eigenvalue {min_eig:.2e} against a largest of "
                f"{max_eig:.2e}), so no robust strength statistic is computed"
            ),
        )
    try:
        pi = np.linalg.pinv(zz) @ (z_within.T @ price_within)
    except np.linalg.LinAlgError:
        return EffectiveF(
            f_eff=None,
            k_eff=None,
            n_absorbed=n_absorbed,
            n_clusters=n_clusters,
            variance_positive_definite=True,
            detail="the first stage could not be solved",
        )
    num = float(pi @ zz @ pi)
    den = float(np.trace(sym @ zz))
    if den <= 0:
        return EffectiveF(
            f_eff=None,
            k_eff=None,
            n_absorbed=n_absorbed,
            n_clusters=n_clusters,
            variance_positive_definite=True,
            detail="the robust denominator is non-positive, so F_eff is undefined",
        )
    f_eff = num / den
    # Effective number of instruments: trace(V Z'Z)^2 / trace((V Z'Z)^2) in
    # the MOP sense, i.e. how many directions actually carry information.
    vz = sym @ zz
    tr1 = float(np.trace(vz))
    tr2 = float(np.trace(vz @ vz))
    k_eff = (tr1 * tr1 / tr2) if tr2 > 0 else None
    return EffectiveF(
        f_eff=f_eff,
        k_eff=k_eff,
        n_absorbed=n_absorbed,
        n_clusters=n_clusters,
        variance_positive_definite=True,
        detail=(
            f"robust effective F {f_eff:.1f} over {n_clusters} independent clusters, "
            f"after absorbing {n_absorbed} fixed-effect parameters"
        ),
    )


@dataclass(frozen=True)
class ArSet:
    """The Anderson-Rubin acceptance set, described as the shape it is."""

    shape: str
    components: list[tuple[float, float]] = field(default_factory=list)
    hull_lo: float | None = None
    hull_hi: float | None = None
    measure: float | None = None  # total accepted width, grid step included
    reaches_lower_bound: bool = False
    reaches_upper_bound: bool = False
    rejected_between: list[tuple[float, float]] = field(default_factory=list)
    display_reliable: bool = True
    detail: str = ""

    @property
    def degenerate(self) -> bool:
        """One or two accepted points: the model is rejected almost
        everywhere, which is evidence AGAINST it, not precision."""
        return self.shape in (AR_SHAPE_EMPTY, AR_SHAPE_POINT)


def ar_set_from_accepted(
    accepted: list[float],
    *,
    grid_lo: float,
    grid_hi: float,
    step: float,
    psd_failures: int = 0,
) -> ArSet:
    """Group accepted grid points into connected components and describe the
    set honestly. A gap wider than one grid step separates components."""
    if not accepted:
        return ArSet(
            shape=AR_SHAPE_EMPTY,
            measure=0.0,
            display_reliable=psd_failures == 0,
            detail=(
                "no candidate elasticity survives the weak-instrument-robust test: "
                "the instruments jointly reject the model everywhere in the search "
                "range, which is evidence of misspecification"
            ),
        )
    pts = sorted(accepted)
    comps: list[tuple[float, float]] = []
    start = prev = pts[0]
    for x in pts[1:]:
        if x - prev > step * 1.5:
            comps.append((start, prev))
            start = x
        prev = x
    comps.append((start, prev))
    # Every component spans at least one grid cell, so a single accepted
    # point has positive measure rather than reading as an empty set.
    measure = sum((hi - lo) + step for lo, hi in comps)
    reaches_lo = pts[0] <= grid_lo + step * 1.01
    reaches_hi = pts[-1] >= grid_hi - step * 1.01
    gaps = [(comps[i][1], comps[i + 1][0]) for i in range(len(comps) - 1)]
    if len(comps) > 1:
        shape = AR_SHAPE_DISCONNECTED
        detail = (
            f"the robust set is {len(comps)} disjoint ranges; the elasticities "
            "between them are rejected, so no single interval describes it"
        )
    elif reaches_lo or reaches_hi:
        shape = AR_SHAPE_REACHES_BOUND
        side = (
            "both ends"
            if reaches_lo and reaches_hi
            else ("the lower end" if reaches_lo else "the upper end")
        )
        detail = (
            f"the robust set runs to {side} of the search range, so it is reported "
            "as open rather than compressed into an interval"
        )
    elif comps[0][0] == comps[0][1]:
        shape = AR_SHAPE_POINT
        detail = (
            "only a single candidate elasticity survives the robust test, which is "
            "evidence the model is rejected almost everywhere rather than evidence "
            "of precision"
        )
    else:
        shape = AR_SHAPE_INTERVAL
        detail = "the robust set is one connected range"
    return ArSet(
        shape=shape,
        components=comps,
        hull_lo=pts[0],
        hull_hi=pts[-1],
        measure=measure,
        reaches_lower_bound=reaches_lo,
        reaches_upper_bound=reaches_hi,
        rejected_between=gaps,
        display_reliable=psd_failures == 0,
        detail=detail
        + (
            f"; {psd_failures} grid point(s) had an inestimable variance and were "
            "skipped, so the displayed extent is narrower than the data support"
            if psd_failures
            else ""
        ),
    )


@dataclass(frozen=True)
class LeadTest:
    """Future instrument values should not predict current quantity."""

    status: str
    wald: float | None = None
    p_value: float | None = None
    dof_num: int | None = None
    dof_den: int | None = None
    coefficients: list[float] = field(default_factory=list)
    ci_low: list[float] = field(default_factory=list)
    ci_high: list[float] = field(default_factory=list)
    horizon: int = 0
    n_rows: int = 0
    detectable_bound: float | None = None
    # Sample composition: the test needs a run of consecutive purchase months
    # per pair, so it runs on frequent repeat buyers rather than the population
    # whose elasticity it gates. Both numbers are reported so that is visible.
    n_positive_rows: int = 0
    residual_dof: int = 0
    detail: str = ""


def lead_test_from_moments(
    *,
    wald: float | None,
    coefficients: np.ndarray | None,
    std_errors: np.ndarray | None,
    dof_num: int,
    dof_den: int,
    horizon: int,
    n_rows: int,
    alpha: float,
    meaningful_effect: float,
    n_positive_rows: int = 0,
    residual_dof: int = 0,
) -> LeadTest:
    """Decide the lead test's outcome from its moments.

    Inference is F(dof_num, dof_den) rather than chi-square: with a few dozen
    clusters a chi-square bar over-rejects, and the rest of the estimator
    already uses t/F critical values for the same reason.

    `meaningful_effect` is the magnitude the engine treats as economically
    material, and it governs BOTH branches. If the interval cannot exclude it,
    a non-rejection is INCONCLUSIVE: a non-rejection of a low-power test is not
    evidence of exogeneity, and recording it as a clean check would be the
    overclaim this phase closes. Symmetrically, if the interval lies entirely
    below it, a rejection is IMMATERIAL: with tens of thousands of rows this
    test resolves effects far below the material threshold, so significance
    alone would move a status on a number the engine calls immaterial.

    It is expressed in log-volume units per unit of the aggregate lead
    regressor, which is one standard deviation of a single lead. For scale, the
    entire contemporaneous price channel on the demo world is about 0.08 in
    those units, so a threshold above roughly 0.1 would be larger than the
    whole effect the instruments identify.
    """

    if wald is None or coefficients is None or std_errors is None or dof_den < 2:
        return LeadTest(
            status=LEAD_UNAVAILABLE,
            horizon=horizon,
            n_rows=n_rows,
            n_positive_rows=n_positive_rows,
            residual_dof=residual_dof,
            detail=(
                "the anticipation test could not be run (too few periods, clusters "
                "or complete rows), so nothing is claimed about anticipation"
            ),
        )
    # The test needs a run of consecutive purchase months per pair, so it runs
    # on frequent repeat buyers rather than on the whole estimation sample. That
    # is stated with every verdict rather than left for a reader to infer.
    if n_positive_rows > 0 and n_rows > 0:
        composition = (
            f" Measured on {n_rows:,} of {n_positive_rows:,} purchase rows - the rows "
            f"with an unbroken run of months around them - leaving {residual_dof:,} "
            "degrees of freedom. Buyers who stop buying altogether are not in this "
            "sample, so anticipation expressed as holding off entirely is invisible "
            "to it."
        )
    else:
        composition = ""
    f_stat = wald / max(dof_num, 1)
    p = float(1.0 - stats.f.cdf(f_stat, dof_num, dof_den))
    crit = float(stats.t.ppf(1 - alpha / 2, dof_den))
    lo = [float(c - crit * s) for c, s in zip(coefficients, std_errors, strict=True)]
    hi = [float(c + crit * s) for c, s in zip(coefficients, std_errors, strict=True)]
    bound = max(abs(x) for x in (*lo, *hi))
    if p < alpha:
        # The SAME materiality standard applies to a rejection as to a
        # non-rejection. At tens of thousands of rows this test resolves effects
        # two orders of magnitude below what the engine calls material, so a
        # bare p < alpha would turn an effect the engine itself treats as
        # immaterial into a status change - reading "anticipation detected"
        # beside an interval that rules the channel out. When the whole interval
        # lies below the material threshold the finding is reported as a
        # detected but immaterial timing effect, and it does not block.
        if bound < meaningful_effect:
            return LeadTest(
                status=LEAD_IMMATERIAL,
                wald=float(wald),
                p_value=p,
                dof_num=dof_num,
                dof_den=dof_den,
                coefficients=[float(c) for c in coefficients],
                ci_low=lo,
                ci_high=hi,
                horizon=horizon,
                n_rows=n_rows,
                detectable_bound=bound,
                detail=(
                    f"future instrument values do predict current quantity "
                    f"(p={p:.4f} over {horizon} lead period(s)), but the whole "
                    f"interval lies below {meaningful_effect:.2f}: the largest "
                    f"anticipation effect it admits is {bound:.3f}, which is "
                    "smaller than the engine treats as material, so the timing "
                    "channel is reported as detected but immaterial" + composition
                ),
            )
        return LeadTest(
            status=LEAD_REJECTED,
            wald=float(wald),
            p_value=p,
            dof_num=dof_num,
            dof_den=dof_den,
            coefficients=[float(c) for c in coefficients],
            ci_low=lo,
            ci_high=hi,
            horizon=horizon,
            n_rows=n_rows,
            n_positive_rows=n_positive_rows,
            residual_dof=residual_dof,
            detectable_bound=bound,
            detail=(
                f"future instrument values predict current quantity (p={p:.4f} over "
                f"{horizon} lead period(s)) by a margin the engine treats as material "
                f"(the interval admits effects up to {bound:.2f}), which is an "
                "exclusion violation the overidentification test cannot see" + composition
            ),
        )
    if bound >= meaningful_effect:
        return LeadTest(
            status=LEAD_INCONCLUSIVE,
            wald=float(wald),
            p_value=p,
            dof_num=dof_num,
            dof_den=dof_den,
            coefficients=[float(c) for c in coefficients],
            ci_low=lo,
            ci_high=hi,
            horizon=horizon,
            n_rows=n_rows,
            n_positive_rows=n_positive_rows,
            residual_dof=residual_dof,
            detectable_bound=bound,
            detail=(
                f"no anticipation effect is detected (p={p:.4f}), but the interval "
                f"still admits effects up to {bound:.2f}, at or beyond the {meaningful_effect:.2f} "
                "the engine treats as material: the test lacks the power to clear "
                "the channel, so it is reported as inconclusive rather than passed" + composition
            ),
        )
    return LeadTest(
        status=LEAD_PASSED,
        wald=float(wald),
        p_value=p,
        dof_num=dof_num,
        dof_den=dof_den,
        coefficients=[float(c) for c in coefficients],
        ci_low=lo,
        ci_high=hi,
        horizon=horizon,
        n_rows=n_rows,
        n_positive_rows=n_positive_rows,
        residual_dof=residual_dof,
        detectable_bound=bound,
        detail=(
            f"future instrument values do not predict current quantity (p={p:.4f}), "
            f"and the interval excludes effects above {bound:.2f} over {horizon} "
            "lead period(s). This tests the ANTICIPATION timing only: a "
            "contemporaneous violation shared by both instruments stays invisible "
            "to it, as it does to the overidentification test." + composition
        ),
    )


def sanderson_windmeijer_residual(
    x_target: np.ndarray, x_other: np.ndarray, z_within: np.ndarray
) -> np.ndarray | None:
    """Residualize one endogenous regressor against the other before measuring
    first-stage strength.

    With two endogenous regressors a marginal first-stage F answers "do the
    instruments move this price?" when the question that matters is "do they
    move this price INDEPENDENTLY of the other one?". A pair of industry-wide
    cost instruments can move both prices strongly and still fail to separate
    them. The Sanderson-Windmeijer construction answers the second question:
    subtract the part of the target price that the other price explains, using
    the instruments to form that relationship, and measure strength on what is
    left. Returns None when the other regressor has no instrumented variation,
    which is itself a failure to separate.
    """
    zz_inv = np.linalg.pinv(z_within.T @ z_within)
    other_hat = z_within @ (zz_inv @ (z_within.T @ x_other))
    den = float(other_hat @ x_other)
    if not np.isfinite(den) or abs(den) <= 0.0:
        return None
    delta = float(other_hat @ x_target) / den
    return np.asarray(x_target - delta * x_other, dtype=float)


PLACEBO_NOT_RUN = "placebo_not_run"
PLACEBO_PASSED = "placebo_passed"
PLACEBO_OVER_REJECTS = "placebo_over_rejects"
PLACEBO_UNDERPOWERED = "placebo_underpowered"


def placebo_verdict(
    share_significant: float | None,
    actual_p: float | None,
    *,
    n_draws: int,
    alpha: float,
) -> tuple[str, str]:
    """Read a shuffled-instrument placebo run as a verdict on the INFERENCE,
    not on relevance.

    The placebo replaces the instruments with shuffled copies and re-estimates.
    Under the null that the inference is correctly sized, each draw rejects with
    probability `alpha`, so the number of rejections is Binomial(n_draws,
    alpha). Rejecting far more often means the standard errors are too small for
    this design - clustering at the wrong level, too few independent clusters -
    and a significant result on the real instruments cannot be trusted either.

    Three outcomes, for the same reason the anticipation test has three: a
    handful of draws cannot distinguish a correctly sized test from a mildly
    over-rejecting one, and reading "2 of 10" as over-rejection would be noise
    reported as a finding. The smallest share the draw count could have flagged
    is reported so a non-rejection is not read as a clean bill of health.
    Relevance is a separate question, answered by the first-stage statistic; the
    real reduced-form p-value is reported here only as context.
    """
    if share_significant is None or n_draws < 2:
        return PLACEBO_NOT_RUN, (
            "the shuffled-instrument placebo has not been run for this segment, "
            "so nothing is claimed about whether the inference is correctly sized"
        )
    rejections = round(share_significant * n_draws)
    p_value = float(stats.binom.sf(rejections - 1, n_draws, alpha)) if rejections else 1.0
    # The smallest rejection count this many draws could call significant, and
    # the share it corresponds to.
    detectable = next(
        (k for k in range(1, n_draws + 1) if float(stats.binom.sf(k - 1, n_draws, alpha)) <= alpha),
        None,
    )
    bound = (detectable / n_draws) if detectable is not None else None
    context = (
        f" The real instruments' reduced form has p = {actual_p:.3f}."
        if actual_p is not None
        else ""
    )
    if p_value <= alpha:
        return PLACEBO_OVER_REJECTS, (
            f"shuffled instruments reached significance on {rejections} of "
            f"{n_draws} draws, which has probability {p_value:.3f} under a "
            f"correctly sized test at {alpha:.0%}. The standard errors in this "
            f"design are too small, so a significant result on the real "
            f"instruments is not trustworthy either." + context
        )
    # Not rejected. Say whether that means anything at this draw count.
    if bound is None or bound > 2.0 * alpha:
        shown = f"{bound:.0%}" if bound is not None else "no share"
        return PLACEBO_UNDERPOWERED, (
            f"shuffled instruments reached significance on {rejections} of "
            f"{n_draws} draws, which is not more than chance, but {n_draws} draws "
            f"could only have flagged {shown} or more against a nominal "
            f"{alpha:.0%}. The check does not clear this design's standard "
            f"errors; it only fails to condemn them." + context
        )
    return PLACEBO_PASSED, (
        f"shuffled instruments reached significance on {rejections} of {n_draws} "
        f"draws, in line with the nominal {alpha:.0%}, and the draw count could "
        f"have flagged {bound:.0%} or more. The inference is correctly sized for "
        f"this design." + context
    )
