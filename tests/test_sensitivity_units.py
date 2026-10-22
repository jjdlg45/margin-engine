"""Run 4 (24.5/24.6/24.7) unit tests for the estimator helpers: multiway
cluster specs, CGM two-way variance, and the identifying-support fields."""

from __future__ import annotations

import numpy as np

from margin_engine.sensitivity.estimate import (
    _cluster_ols,
    _make_cluster_spec,
    _spec_min_groups,
)


def test_one_way_spec_single_term() -> None:
    a = np.array(["m1", "m1", "m2", "m2", "m3"])
    spec = _make_cluster_spec(a)
    assert len(spec) == 1
    _codes, n_g, sign = spec[0]
    assert n_g == 3
    assert sign == +1
    assert _spec_min_groups(spec) == 3


def test_two_way_spec_cgm_terms() -> None:
    months = np.array(["m1", "m1", "m2", "m2"])
    custs = np.array(["c1", "c2", "c1", "c2"])
    spec = _make_cluster_spec(months, custs)
    assert [t[2] for t in spec] == [+1, +1, -1]
    assert spec[0][1] == 2  # months
    assert spec[1][1] == 2  # customers
    assert spec[2][1] == 4  # intersections
    assert _spec_min_groups(spec) == 2  # smallest positive-sign dimension


def test_cluster_ols_beta_is_exact_ols() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(200, 1))
    y = 2.0 * x[:, 0] + rng.normal(size=200)
    clusters = np.repeat([f"g{i}" for i in range(20)], 10)
    beta, vcov = _cluster_ols(y, x, _make_cluster_spec(clusters))
    expected = float(np.linalg.lstsq(x, y, rcond=None)[0][0])
    np.testing.assert_allclose(beta[0], expected, rtol=1e-10)
    assert vcov[0, 0] > 0


def test_two_way_degenerate_equals_one_way() -> None:
    """When both dimensions are the SAME partition, CGM gives V + V - V = V."""
    rng = np.random.default_rng(1)
    x = rng.normal(size=(120, 1))
    y = -1.0 * x[:, 0] + rng.normal(size=120)
    g = np.repeat([f"g{i}" for i in range(12)], 10)
    _, v1 = _cluster_ols(y, x, _make_cluster_spec(g))
    _, v2 = _cluster_ols(y, x, _make_cluster_spec(g, g))
    np.testing.assert_allclose(v2[0, 0], v1[0, 0], rtol=1e-10)


def test_two_way_captures_persistent_customer_correlation() -> None:
    """With a strong customer random effect in the residual, month-only
    clustering understates the variance; month x customer two-way does not."""
    rng = np.random.default_rng(42)
    n_cust, n_months = 40, 24
    cust = np.repeat(np.arange(n_cust), n_months)
    month = np.tile(np.arange(n_months), n_cust)
    x = rng.normal(size=cust.shape[0])
    # Residual: large persistent customer effect + small idiosyncratic noise,
    # and x itself has a customer-level component so the score is clustered.
    x = x + 1.0 * rng.normal(size=n_cust)[cust]
    u = 2.0 * rng.normal(size=n_cust)[cust] + 0.3 * rng.normal(size=cust.shape[0])
    y = 1.5 * x + u
    xm = (x - x.mean()).reshape(-1, 1)
    ym = y - y.mean()
    m_lab = np.array([f"m{t}" for t in month])
    c_lab = np.array([f"c{c}" for c in cust])
    _, v_month = _cluster_ols(ym, xm, _make_cluster_spec(m_lab))
    _, v_two = _cluster_ols(ym, xm, _make_cluster_spec(m_lab, c_lab))
    assert v_two[0, 0] > 2.0 * v_month[0, 0], (v_two[0, 0], v_month[0, 0])
