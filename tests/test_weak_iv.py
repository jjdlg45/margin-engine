"""Phase 71 (Run 6): robust weak-instrument diagnostics.

Findings 4 and 13. Three properties under test: the effective first-stage F
is computed on a corrected variance and refuses a broken one; the
Anderson-Rubin acceptance set is described as the SHAPE it is rather than
compressed into an interval; and the anticipation (lead) test distinguishes a
rejection from a merely underpowered non-rejection.
"""

from __future__ import annotations

import numpy as np
import pytest

from margin_engine.sensitivity.weak_iv import (
    AR_SHAPE_DISCONNECTED,
    AR_SHAPE_EMPTY,
    AR_SHAPE_INTERVAL,
    AR_SHAPE_POINT,
    AR_SHAPE_REACHES_BOUND,
    LEAD_IMMATERIAL,
    LEAD_INCONCLUSIVE,
    LEAD_PASSED,
    LEAD_REJECTED,
    LEAD_UNAVAILABLE,
    MOP_CRITICAL_VALUE_10PCT_BIAS,
    PLACEBO_NOT_RUN,
    PLACEBO_OVER_REJECTS,
    PLACEBO_PASSED,
    PLACEBO_UNDERPOWERED,
    ar_set_from_accepted,
    effective_first_stage_f,
    lead_test_from_moments,
    placebo_verdict,
    sanderson_windmeijer_residual,
)

RNG = np.random.default_rng(11)


# ---------------------------------------------------------------------------
# Effective first-stage F
# ---------------------------------------------------------------------------


def _first_stage(n: int = 500, strength: float = 0.8, k: int = 2):
    z = RNG.normal(0, 1, (n, k))
    p = z @ np.full(k, strength) + RNG.normal(0, 1, n)
    return p, z


def test_effective_f_matches_the_homoskedastic_reduction() -> None:
    """With V = sigma^2 (Z'Z)^-1 the statistic reduces to
    pi'Z'Z pi / (K sigma^2) - the classical first-stage F times K/K."""
    p, z = _first_stage()
    zz = z.T @ z
    pi = np.linalg.solve(zz, z.T @ p)
    resid = p - z @ pi
    sigma2 = float(resid @ resid) / (len(p) - z.shape[1])
    v = sigma2 * np.linalg.inv(zz)
    out = effective_first_stage_f(p, z, v, n_absorbed=0, n_clusters=30)
    expected = float(pi @ zz @ pi) / float(np.trace(v @ zz))
    assert out.f_eff == pytest.approx(expected)
    # trace(V Z'Z) = K sigma^2 under homoskedasticity.
    assert float(np.trace(v @ zz)) == pytest.approx(z.shape[1] * sigma2)


def test_effective_f_is_scale_invariant_in_the_instruments() -> None:
    p, z = _first_stage()
    zz = z.T @ z
    pi = np.linalg.solve(zz, z.T @ p)
    resid = p - z @ pi
    sigma2 = float(resid @ resid) / (len(p) - 2)
    a = effective_first_stage_f(p, z, sigma2 * np.linalg.inv(zz), n_absorbed=0, n_clusters=30)
    z2 = z * 10.0
    zz2 = z2.T @ z2
    b = effective_first_stage_f(p, z2, sigma2 * np.linalg.inv(zz2), n_absorbed=0, n_clusters=30)
    assert a.f_eff == pytest.approx(b.f_eff, rel=1e-9)


def test_non_positive_definite_variance_is_refused_not_reported() -> None:
    """A Cameron-Gelbach-Miller combination can leave a positive trace while
    not being positive definite; that must not read as strength."""
    p, z = _first_stage()
    bad = np.array([[1.0, 0.0], [0.0, -0.5]])  # positive trace, negative eigenvalue
    out = effective_first_stage_f(p, z, bad, n_absorbed=0, n_clusters=30)
    assert out.f_eff is None
    assert not out.variance_positive_definite
    assert "not positive definite" in out.detail


def test_inestimable_variance_is_refused() -> None:
    p, z = _first_stage()
    out = effective_first_stage_f(
        p, z, np.array([[np.inf, 0.0], [0.0, np.inf]]), n_absorbed=0, n_clusters=30
    )
    assert out.f_eff is None
    assert "not estimable" in out.detail


def test_weaker_instruments_give_a_smaller_effective_f() -> None:
    strong_p, strong_z = _first_stage(strength=0.8)
    weak_p, weak_z = _first_stage(strength=0.02)

    def f_of(p: np.ndarray, z: np.ndarray) -> float:
        zz = z.T @ z
        pi = np.linalg.solve(zz, z.T @ p)
        resid = p - z @ pi
        s2 = float(resid @ resid) / (len(p) - z.shape[1])
        out = effective_first_stage_f(p, z, s2 * np.linalg.inv(zz), n_absorbed=0, n_clusters=30)
        assert out.f_eff is not None
        return out.f_eff

    assert f_of(strong_p, strong_z) > f_of(weak_p, weak_z)


def test_absorbed_parameters_are_reported() -> None:
    p, z = _first_stage()
    zz = z.T @ z
    out = effective_first_stage_f(p, z, np.linalg.inv(zz), n_absorbed=1234, n_clusters=24)
    assert out.n_absorbed == 1234
    assert out.n_clusters == 24
    assert "1234" in out.detail


def test_the_published_bar_is_the_one_derived_value() -> None:
    assert pytest.approx(23.109) == MOP_CRITICAL_VALUE_10PCT_BIAS


# ---------------------------------------------------------------------------
# Anderson-Rubin set shape
# ---------------------------------------------------------------------------


def test_empty_set_is_evidence_of_misspecification() -> None:
    s = ar_set_from_accepted([], grid_lo=-5.0, grid_hi=1.0, step=0.02)
    assert s.shape == AR_SHAPE_EMPTY
    assert s.measure == 0.0
    assert s.degenerate
    assert "reject the model everywhere" in s.detail


def test_single_point_has_positive_measure_and_is_degenerate() -> None:
    """A one-point set must not report width zero: that reads as 'nothing is
    accepted' when the truth is 'almost nothing is accepted'."""
    s = ar_set_from_accepted([-1.5], grid_lo=-5.0, grid_hi=1.0, step=0.02)
    assert s.shape == AR_SHAPE_POINT
    assert s.measure == pytest.approx(0.02)
    assert s.degenerate
    assert s.hull_lo == s.hull_hi == -1.5


def test_connected_interval() -> None:
    pts = [round(-2.0 + 0.02 * i, 10) for i in range(11)]
    s = ar_set_from_accepted(pts, grid_lo=-5.0, grid_hi=1.0, step=0.02)
    assert s.shape == AR_SHAPE_INTERVAL
    assert s.hull_lo == pytest.approx(-2.0)
    assert s.hull_hi == pytest.approx(-1.8)
    assert s.measure == pytest.approx(0.22)
    assert not s.degenerate
    assert s.rejected_between == []


def test_disconnected_set_reports_the_rejected_gap() -> None:
    pts = [-2.0, -1.98, -1.96, -1.0, -0.98]
    s = ar_set_from_accepted(pts, grid_lo=-5.0, grid_hi=1.0, step=0.02)
    assert s.shape == AR_SHAPE_DISCONNECTED
    assert len(s.components) == 2
    assert s.rejected_between == [(-1.96, -1.0)]
    # The measure counts only the accepted ranges, not the gap.
    assert s.measure == pytest.approx(0.04 + 0.02 + 0.02 + 0.02)
    assert "no single interval describes it" in s.detail


def test_edge_contact_is_reported_as_reaching_the_bound() -> None:
    pts = [-5.0, -4.98, -4.96]
    s = ar_set_from_accepted(pts, grid_lo=-5.0, grid_hi=1.0, step=0.02)
    assert s.shape == AR_SHAPE_REACHES_BOUND
    assert s.reaches_lower_bound and not s.reaches_upper_bound
    assert "search range" in s.detail


def test_skipped_grid_points_make_the_display_unreliable() -> None:
    pts = [-2.0, -1.98]
    s = ar_set_from_accepted(pts, grid_lo=-5.0, grid_hi=1.0, step=0.02, psd_failures=7)
    assert not s.display_reliable
    assert "narrower than the data support" in s.detail


# ---------------------------------------------------------------------------
# Anticipation (lead) test
# ---------------------------------------------------------------------------


def _lead(**kw: object):
    args: dict[str, object] = {
        "wald": 0.4,
        "coefficients": np.array([0.01]),
        "std_errors": np.array([0.03]),
        "dof_num": 1,
        "dof_den": 23,
        "horizon": 2,
        "n_rows": 5000,
        "alpha": 0.05,
        "meaningful_effect": 0.5,
        "n_positive_rows": 0,
        "residual_dof": 0,
    }
    args.update(kw)
    return lead_test_from_moments(**args)  # type: ignore[arg-type]


def test_tight_non_rejection_passes() -> None:
    t = _lead()
    assert t.status == LEAD_PASSED
    assert t.detectable_bound is not None and t.detectable_bound < 0.5
    # The pass is explicit about what it does NOT cover.
    assert "contemporaneous violation" in t.detail


def test_rejection_is_called_an_exclusion_violation() -> None:
    t = _lead(wald=25.0, coefficients=np.array([0.4]), std_errors=np.array([0.08]))
    assert t.status == LEAD_REJECTED
    assert t.p_value is not None and t.p_value < 0.05
    assert "overidentification test cannot see" in t.detail


def test_wide_non_rejection_is_inconclusive_not_a_pass() -> None:
    """A non-rejection of a low-power test is not evidence of exogeneity."""
    t = _lead(wald=0.1, coefficients=np.array([0.05]), std_errors=np.array([2.0]))
    assert t.status == LEAD_INCONCLUSIVE
    assert t.detectable_bound is not None and t.detectable_bound >= 0.5
    assert "lacks the power" in t.detail


def test_missing_moments_are_unavailable_not_passed() -> None:
    assert _lead(wald=None).status == LEAD_UNAVAILABLE
    assert _lead(dof_den=1).status == LEAD_UNAVAILABLE
    assert "nothing is claimed" in _lead(wald=None).detail


def test_inference_uses_f_not_chi_square() -> None:
    """With 23 denominator degrees of freedom the F bar is strictly more
    conservative than chi-square, which is why it is used."""
    from scipy import stats

    wald = 4.0
    t = _lead(wald=wald, coefficients=np.array([0.2]), std_errors=np.array([0.1]))
    chi2_p = float(1 - stats.chi2.cdf(wald, 1))
    assert t.p_value is not None
    assert t.p_value > chi2_p


def test_horizon_is_reported() -> None:
    t = _lead(horizon=3)
    assert t.horizon == 3
    assert "3 lead period" in t.detail


# ---------------------------------------------------------------------------
# Conditional (Sanderson-Windmeijer) first stage
# ---------------------------------------------------------------------------


def _two_endogenous(n: int = 800, other_loading: float = 0.9):
    """Two prices driven by the same two instruments. The first price always
    loads (1.0, 0.3); `other_loading` sets the second price's loading on the
    second instrument. At 0.3 the two loading vectors are parallel, so no
    combination of instruments moves one price without the other."""
    z = RNG.normal(0, 1, (n, 2))
    x1 = z[:, 0] + 0.3 * z[:, 1] + RNG.normal(0, 0.5, n)
    x2 = z[:, 0] + other_loading * z[:, 1] + RNG.normal(0, 0.5, n)
    return x1, x2, z


def test_conditional_residual_is_weak_when_instruments_do_not_separate() -> None:
    """Both prices move strongly with the instruments either way; only the
    conditional statistic distinguishes 'moves it' from 'moves it apart'."""

    def strength(other_loading: float) -> tuple[float, float]:
        x1, x2, z = _two_endogenous(other_loading=other_loading)
        zz = z.T @ z
        zz_inv = np.linalg.pinv(zz)
        pi = zz_inv @ (z.T @ x1)
        marginal = float(pi @ zz @ pi)
        sw = sanderson_windmeijer_residual(x1, x2, z)
        assert sw is not None
        pi_sw = zz_inv @ (z.T @ sw)
        return marginal, float(pi_sw @ zz @ pi_sw)

    m_sep, c_sep = strength(0.9)
    m_same, c_same = strength(0.3)
    # Marginal strength is essentially unchanged; conditional strength
    # collapses once the two loading vectors point the same way.
    assert m_same > 0.5 * m_sep
    assert c_same < 0.1 * c_sep


def test_conditional_residual_refuses_an_uninstrumented_other_price() -> None:
    x1, _x2, z = _two_endogenous()
    assert sanderson_windmeijer_residual(x1, np.zeros_like(x1), z) is None


def test_rounding_floor_eigenvalue_is_not_treated_as_indefinite() -> None:
    """A positive-semidefinite sandwich on tiny within-transformed values can
    land a hair below zero. Refusing that withholds a usable statistic; only
    material indefiniteness is refused."""
    p, z = _first_stage()
    zz = z.T @ z
    v = np.linalg.inv(zz)
    noise = float(np.abs(np.linalg.eigvalsh(v)).max()) * 1e-14
    near = v - np.eye(2) * (float(np.linalg.eigvalsh(v).min()) + noise)
    out = effective_first_stage_f(p, z, near, n_absorbed=0, n_clusters=30)
    assert out.f_eff is not None
    material = v - np.eye(2) * (float(np.linalg.eigvalsh(v).min()) * 2.0)
    bad = effective_first_stage_f(p, z, material, n_absorbed=0, n_clusters=30)
    assert bad.f_eff is None
    assert "smallest eigenvalue" in bad.detail


# ---------------------------------------------------------------------------
# Shuffled-instrument placebo verdict
# ---------------------------------------------------------------------------


def test_two_of_ten_draws_is_sampling_noise_not_over_rejection() -> None:
    """At ten draws, two rejections has probability 0.09 under a correctly
    sized test. Calling that over-rejection reports noise as a finding."""
    status, detail = placebo_verdict(0.2, 0.0, n_draws=10, alpha=0.05)
    assert status == PLACEBO_UNDERPOWERED
    assert "not more than chance" in detail
    # It says what it could have caught, so silence is not read as a pass.
    assert "30%" in detail


def test_four_of_ten_draws_is_over_rejection() -> None:
    status, detail = placebo_verdict(0.4, 0.0, n_draws=10, alpha=0.05)
    assert status == PLACEBO_OVER_REJECTS
    assert "not trustworthy" in detail


def test_enough_draws_can_actually_clear_the_inference() -> None:
    """With 300 draws the test can flag a share close to nominal, so a
    non-rejection is informative and reported as a pass."""
    status, detail = placebo_verdict(0.05, 0.001, n_draws=300, alpha=0.05)
    assert status == PLACEBO_PASSED
    assert "correctly sized" in detail


def test_placebo_without_a_run_claims_nothing() -> None:
    assert placebo_verdict(None, None, n_draws=10, alpha=0.05)[0] == PLACEBO_NOT_RUN
    assert placebo_verdict(0.2, None, n_draws=0, alpha=0.05)[0] == PLACEBO_NOT_RUN


def test_placebo_verdict_is_monotone_in_the_rejection_count() -> None:
    """More shuffled rejections must never read as better-sized inference."""
    order = {PLACEBO_PASSED: 0, PLACEBO_UNDERPOWERED: 1, PLACEBO_OVER_REJECTS: 2}
    seen = [order[placebo_verdict(k / 50, 0.0, n_draws=50, alpha=0.05)[0]] for k in range(0, 26)]
    assert seen == sorted(seen)


# ---------------------------------------------------------------------------
# Materiality applies to BOTH branches of the anticipation test
# ---------------------------------------------------------------------------


def test_significant_but_immaterial_anticipation_does_not_block() -> None:
    """At tens of thousands of rows the test resolves effects far below what
    the engine calls material. Significance alone would move a status on a
    number the engine itself treats as immaterial, while printing "anticipation
    detected" beside an interval that rules the channel out."""
    t = _lead(
        wald=6.0,
        coefficients=np.array([-0.00405]),
        std_errors=np.array([0.00188]),
        n_rows=80_000,
        meaningful_effect=0.05,
    )
    assert t.status == LEAD_IMMATERIAL
    assert t.p_value is not None and t.p_value < 0.05
    # The whole interval sits below the material threshold.
    assert t.detectable_bound is not None and t.detectable_bound < 0.05
    assert "smaller than the engine treats as material" in t.detail


def test_significant_and_material_anticipation_still_rejects() -> None:
    t = _lead(
        wald=30.0,
        coefficients=np.array([-0.4]),
        std_errors=np.array([0.07]),
        meaningful_effect=0.05,
    )
    assert t.status == LEAD_REJECTED
    assert "overidentification test cannot see" in t.detail


def test_the_two_branches_use_one_materiality_standard() -> None:
    """Sweeping precision at a fixed effect size must not flip the verdict
    between 'material violation' and 'clean' without passing through the
    immaterial or inconclusive readings."""
    seen = []
    for se in (0.5, 0.1, 0.02, 0.004, 0.001):
        t = _lead(
            wald=(0.03 / se) ** 2,
            coefficients=np.array([0.03]),
            std_errors=np.array([se]),
            meaningful_effect=0.05,
        )
        seen.append(t.status)
    # A 0.03 effect is below the 0.05 bar, so no precision should ever make it
    # read as a material violation.
    assert LEAD_REJECTED not in seen, seen


def test_sample_composition_is_reported() -> None:
    t = _lead(n_rows=8_441, n_positive_rows=16_246, residual_dof=7_900)
    assert t.n_rows == 8_441
    assert t.n_positive_rows == 16_246
    assert t.residual_dof == 7_900


# ---------------------------------------------------------------------------
# Clustered OLS must refuse a variance it cannot estimate
# ---------------------------------------------------------------------------


def test_saturated_design_reports_no_variance_rather_than_a_tiny_one() -> None:
    """When the fixed effects absorb more parameters than there are rows the
    residuals collapse to the rounding floor. The small-sample factor used to
    switch itself off at exactly that point, so the variance came back at 1e-12
    to 1e-24 and any coefficient became an enormous Wald statistic. Measured on
    thin segments, that produced rejection rates of 8-30% with nothing planted.
    """
    from margin_engine.sensitivity.estimate import _cluster_ols, _make_cluster_spec

    n, k = 60, 5
    x = RNG.normal(0, 1, (n, k))
    y = RNG.normal(0, 1, n)
    spec = _make_cluster_spec(RNG.integers(0, 24, n))
    # 70 absorbed parameters against 60 rows: nothing is left to estimate with.
    _beta, vcov = _cluster_ols(y, x, spec, 70)
    assert not np.any(np.isfinite(np.diag(vcov)))
    # With room to spare the variance is finite and positive.
    _beta2, vcov2 = _cluster_ols(y, x, spec, 5)
    assert np.all(np.isfinite(np.diag(vcov2)))
    assert np.all(np.diag(vcov2) > 0)


def test_non_positive_clustered_variance_is_caught_above_one_regressor() -> None:
    """The Cameron-Gelbach-Miller combination subtracts one sandwich from
    another and can leave a non-positive diagonal entry at any number of
    regressors. The guard used to be reachable only with a single regressor, so
    a five-regressor design could report a zero variance as precision."""
    from margin_engine.sensitivity.estimate import _cluster_ols, _make_cluster_spec

    n, k = 2000, 5
    x = RNG.normal(0, 1, (n, k))
    y = x @ np.full(k, 0.3) + RNG.normal(0, 1, n)
    # Two-way clustering on the same variable makes the third CGM term cancel
    # the first two exactly, which is where non-positive entries come from.
    g = RNG.integers(0, 20, n)
    spec = _make_cluster_spec(g, g)
    _beta, vcov = _cluster_ols(y, x, spec, 0)
    diag = np.diag(vcov)
    # Every reported variance is either strictly positive or declared
    # inestimable; none is zero or negative.
    assert all(v > 0 or not np.isfinite(v) for v in diag), diag
