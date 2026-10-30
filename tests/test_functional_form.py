"""Is one elasticity enough for a segment?

A log-log model reports a single number and the optimizer prices a move with
it. On a world with curved demand every other diagnostic passes - strong first
stage, overidentification not rejected, tight robust set - and the estimate
still misses the planted local elasticity by about 0.3, because it is an
average over the prices in the data rather than the elasticity at the price
being moved to.

The check re-estimates the segment on its cheaper-priced and its dearer-priced
pairs and compares. These tests cover what it must get right: that it splits on
a PAIR characteristic rather than on each row's own price, that a difference
has to be both material and bigger than the comparison can resolve before it
withdraws anything, and that it says so when it cannot tell.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl
import pytest

from margin_engine.config import Rules
from margin_engine.labels import LABELS
from margin_engine.sensitivity.estimate import (
    FF_CONSTANT_SUPPORTED,
    FF_INCONCLUSIVE,
    FF_NOT_TESTED,
    FF_PRICE_DEPENDENT,
    GATE_FAIL,
    GATE_PASS,
    STATUS_CAUSAL,
    _functional_form_check,
    estimate_segment,
)

CLUSTER = "cluster_supplier_month"
TIME_FE = "role_month"


def _world(
    *,
    beta_low: float,
    beta_high: float,
    n_pairs: int = 80,
    n_months: int = 24,
    noise: float = 0.10,
    price_sd: float = 0.5,
    level_coef: float = 0.05,
    seed: int = 0,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    """A segment whose cheaper half and dearer half have their own elasticity.

    The arrays are already within-transformed, as `estimate_segment` hands
    them over: `price` is the residualized log price, `z` the instrument, `y`
    the residualized log quantity. `log_price` in the frame is the LEVEL,
    which is what the split reads.
    """
    rng = np.random.default_rng(seed)
    n_items = 20
    pairs, items, months, level, y, price, z = [], [], [], [], [], [], []
    for p in range(n_pairs):
        item = p % n_items
        # The dearer half sits 10% above its item's average, the cheaper half
        # 10% below. Items get pairs from both halves, so the position is
        # within-item and not an item effect in disguise.
        offset = 0.10 if p >= n_pairs // 2 else -0.10
        beta = beta_high if p >= n_pairs // 2 else beta_low
        for m in range(n_months):
            zi = rng.normal()
            pi = price_sd * zi + rng.normal(0, 0.05)
            pairs.append(f"c{p}")
            items.append(f"i{item}")
            months.append(m)
            level.append(np.log(100.0 + item) + offset + level_coef * pi)
            price.append(pi)
            z.append(zi)
            y.append(beta * pi + rng.normal(0, noise))
    pdf = pd.DataFrame(
        {
            "log_price": level,
            "pair": pairs,
            "item_id": items,
            CLUSTER: [f"s{i % 12}@{m}" for i, m in enumerate(months)],
            TIME_FE: [f"all@{m}" for m in months],
        }
    )
    return pdf, np.array(y), np.array(price), np.array(z).reshape(-1, 1)


def _check(pdf: pd.DataFrame, y, price, z, rules: Rules | None = None):
    return _functional_form_check(pdf, y, price, z, [CLUSTER], TIME_FE, rules or Rules())


# ---------------------------------------------------------------------------
# What it concludes
# ---------------------------------------------------------------------------


def test_one_elasticity_everywhere_is_supported() -> None:
    ff = _check(*_world(beta_low=-1.5, beta_high=-1.5))
    assert ff.status == FF_CONSTANT_SUPPORTED
    assert ff.gap is not None and ff.gap < 0.30
    # A pass is only worth something if the comparison could have failed.
    assert ff.bound is not None and ff.bound <= 0.30
    assert "would not have been told from noise" in ff.detail


def test_an_elasticity_that_moves_with_the_price_level_is_refused() -> None:
    ff = _check(*_world(beta_low=-1.0, beta_high=-2.0))
    assert ff.status == FF_PRICE_DEPENDENT
    assert ff.gap is not None and ff.gap > 0.30
    assert ff.bound is not None and ff.gap > ff.bound
    assert "more price-sensitive where prices are higher" in ff.detail
    assert "local average" in ff.detail


def test_the_two_halves_are_named_with_their_price_positions() -> None:
    ff = _check(*_world(beta_low=-1.5, beta_high=-1.5))
    assert "below their item's average give" in ff.detail
    assert "above give" in ff.detail
    assert ff.n_low == ff.n_high  # the split is even when the halves are


# ---------------------------------------------------------------------------
# What it refuses to conclude
# ---------------------------------------------------------------------------


def test_a_difference_it_cannot_resolve_is_not_a_finding() -> None:
    """The same curvature, measured on a panel too noisy to see it. The gap
    clears the maximum and the comparison still cannot tell it from nothing,
    so nothing is concluded - the failure mode that would otherwise withdraw a
    status on noise."""
    ff = _check(*_world(beta_low=-1.0, beta_high=-2.0, n_pairs=44, n_months=12, noise=2.5))
    assert ff.status == FF_INCONCLUSIVE
    assert ff.bound is not None and ff.gap is not None and ff.bound > ff.gap
    assert "less than this comparison can resolve" in ff.detail


def test_agreement_between_two_vague_numbers_is_not_agreement() -> None:
    """Both halves give the same elasticity, but so imprecisely that a
    material difference would have gone unseen. That is not evidence of a
    constant elasticity, and the status says so."""
    ff = _check(*_world(beta_low=-1.5, beta_high=-1.5, n_pairs=44, n_months=12, noise=1.5))
    assert ff.status == FF_INCONCLUSIVE
    assert ff.gap is not None and ff.gap <= 0.30
    assert ff.bound is not None and ff.bound > 0.30
    assert "could not have shown a difference as large as the maximum" in ff.detail


def test_a_half_with_too_little_data_stops_the_comparison() -> None:
    ff = _check(*_world(beta_low=-1.0, beta_high=-2.0, n_pairs=10, n_months=4))
    assert ff.status == FF_INCONCLUSIVE
    assert "under the" in ff.detail and "pairs the comparison needs" in ff.detail


def test_a_half_whose_instrument_barely_moves_is_not_compared() -> None:
    """A weak half cannot estimate anything of its own, so the comparison has
    nothing to compare and says that rather than reading the noise."""
    pdf, y, price, z = _world(beta_low=-1.5, beta_high=-1.5)
    dear = (pdf["pair"].str[1:].astype(int) >= 40).to_numpy()
    # Noise in place of the instrument: the dearer half's first stage goes to
    # nothing. Scaling the instrument would not do it - the F statistic does
    # not care about the units of z.
    z = z.copy()
    z[dear] = np.random.default_rng(7).normal(size=(int(dear.sum()), 1))
    ff = _check(pdf, y, price, z)
    assert ff.status == FF_INCONCLUSIVE
    assert "first-stage F" in ff.detail


def test_every_pair_sitting_at_the_same_price_has_no_halves() -> None:
    pdf, y, price, z = _world(beta_low=-1.5, beta_high=-1.5)
    pdf = pdf.assign(log_price=4.6)
    ff = _check(pdf, y, price, z)
    assert ff.status == FF_INCONCLUSIVE
    assert "no cheaper and dearer halves" in ff.detail


# ---------------------------------------------------------------------------
# When it does not run at all
# ---------------------------------------------------------------------------


def test_the_check_can_be_switched_off_in_config() -> None:
    rules = Rules.model_validate({"sensitivity": {"functional_form_check_enabled": False}})
    ff = _check(*_world(beta_low=-1.0, beta_high=-2.0), rules=rules)
    assert ff.status == FF_NOT_TESTED
    assert "off in config" in ff.detail


def test_without_an_item_identifier_a_price_cannot_be_placed() -> None:
    pdf, y, price, z = _world(beta_low=-1.0, beta_high=-2.0)
    ff = _check(pdf.drop(columns=["item_id"]), y, price, z)
    assert ff.status == FF_NOT_TESTED
    assert "no item identifier" in ff.detail


# ---------------------------------------------------------------------------
# How it splits
# ---------------------------------------------------------------------------


def test_the_split_is_on_the_pair_not_on_each_row_price() -> None:
    """Splitting on a row's own price would select on the endogenous
    regressor, and with it on the demand shock. Every row of a pair has to
    land on the same side, however much the pair's price moves over time."""
    pdf, y, price, z = _world(beta_low=-1.5, beta_high=-1.5, price_sd=0.5)
    # Row-level prices overlap heavily between the halves: the within-pair
    # movement is wider than the 20% gap between the pair averages.
    pdf = pdf.assign(log_price=pdf["log_price"] + 0.5 * price)
    ff = _check(pdf, y, price, z)
    assert ff.n_low == ff.n_high == len(pdf) // 2
    low_rows = int((pdf["pair"].str[1:].astype(int) < 40).sum())
    assert ff.n_low == low_rows


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_a_constant_elasticity_world_is_not_refused_by_chance(seed: int) -> None:
    """The gate withdraws a status, so a false refusal costs something. On
    five constant-elasticity draws it must never refuse."""
    ff = _check(*_world(beta_low=-1.4, beta_high=-1.4, seed=seed))
    assert ff.status != FF_PRICE_DEPENDENT


# ---------------------------------------------------------------------------
# In the ladder
# ---------------------------------------------------------------------------


def _panel(beta_low: float, beta_high: float, *, seed: int = 0) -> pl.DataFrame:
    """A panel in the shape `estimate_segment` reads, with two instruments."""
    # `level_coef=1` so the within-pair price movement in the frame IS the
    # residualized price the estimator will recover an elasticity from, and a
    # narrow spread so a pair's average still lands it on the right side of
    # the split rather than its own month-to-month noise.
    pdf, y, price, z = _world(
        beta_low=beta_low,
        beta_high=beta_high,
        n_pairs=90,
        n_months=24,
        price_sd=0.15,
        level_coef=1.0,
        seed=seed,
    )
    rng = np.random.default_rng(seed + 100)
    z2 = 0.6 * z[:, 0] + rng.normal(0, 0.5, len(price))
    month = [int(c.split("@")[1]) for c in pdf[TIME_FE]]
    return pl.DataFrame(
        {
            "pair": pdf["pair"].tolist(),
            "customer_id": pdf["pair"].tolist(),
            "item_id": pdf["item_id"].tolist(),
            "month_idx": month,
            "contract_id": [None] * len(price),
            "log_price": pdf["log_price"].to_numpy(),
            "qty": np.exp(y - y.min() + 1.0),
            "log_qty": y - y.min() + 1.0,
            "z_cost": z[:, 0],
            "z_tariff": z2,
            "cluster_supplier": [f"s{i % 12}" for i in range(len(price))],
            "cluster_supplier_month": pdf[CLUSTER].tolist(),
            "cluster_category": [f"g{i % 6}" for i in range(len(price))],
            "cluster_category_month": [f"g{i % 6}@{m}" for i, m in enumerate(month)],
            "cluster_month": [f"{m}" for m in month],
            "cluster_month_customer": [f"{m}" for m in month],
            "role_month": pdf[TIME_FE].tolist(),
        }
    )


def test_the_criterion_is_in_the_ladder_and_names_itself() -> None:
    est = estimate_segment(_panel(-1.5, -1.5), Rules(), 1, with_total_quantity=False)
    gates = {g.gate: g for g in est.causal_status_reasons}
    assert "constant_elasticity_supported" in gates, (
        f"the functional-form criterion is missing from the ladder: {sorted(gates)}"
    )
    assert gates["constant_elasticity_supported"].order == 11
    assert est.functional_form_status == FF_CONSTANT_SUPPORTED
    assert gates["constant_elasticity_supported"].result == GATE_PASS


def test_curvature_withdraws_the_top_status_and_says_why() -> None:
    est = estimate_segment(_panel(-1.0, -2.0), Rules(), 1, with_total_quantity=False)
    gates = {g.gate: g for g in est.causal_status_reasons}
    assert est.functional_form_status == FF_PRICE_DEPENDENT
    assert gates["constant_elasticity_supported"].result == GATE_FAIL
    assert est.status != STATUS_CAUSAL
    assert any("depends on the price level" in r for r in est.status_reasons)
    # The failure is machine-readable for the UI, not only prose.
    assert "constant_elasticity_supported" in est.to_dict()["causal_gate_fail_codes"]


def test_every_outcome_has_plain_english_to_show_for_it() -> None:
    """Nothing a reader sees may be a raw code (Run 6, Phase 46)."""
    for code in (
        "constant_elasticity_supported",
        FF_PRICE_DEPENDENT,
        FF_INCONCLUSIVE,
        FF_NOT_TESTED,
    ):
        assert code in LABELS, f"{code} has no label, so the UI would print the raw code"
        assert LABELS[code]["label"] and LABELS[code]["description"]
        assert "_" not in LABELS[code]["label"]
