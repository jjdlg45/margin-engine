"""Run 6, Phase 78 (Finding 12): the statistics behind the validation rows.

A validation table that says "Median 0.019" and nothing else invites the
reader to treat it as exact. Two things make it honest: an interval that
resamples the right unit, and seeds that nobody tuned against.

The unit matters more than it looks. Every segment inside one synthetic
world shares that world's demand shocks, instruments and prices, so
resampling segments treats correlated draws as independent and reports an
interval far tighter than the evidence supports. These tests measure that
difference rather than asserting it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_script():  # type: ignore[no-untyped-def]
    """Import measure_validation without running its heavy module body twice."""
    name = "measure_validation_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "measure_validation.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mv = _load_script()


def test_the_held_out_seeds_are_not_the_development_seeds() -> None:
    assert not set(mv.HELD_OUT_SEEDS) & set(mv.DEV_SEEDS)
    # Nor the coverage study's range, or "held out" would be a word only.
    assert not set(mv.HELD_OUT_SEEDS) & set(range(100, 150))
    assert len(mv.HELD_OUT_SEEDS) >= 10


def test_an_interval_needs_more_than_two_worlds() -> None:
    assert mv.world_bootstrap({1: [0.1], 2: [0.2]}, np.median) is None
    assert mv.world_bootstrap({}, np.median) is None


def test_the_interval_brackets_the_statistic() -> None:
    rng = np.random.default_rng(0)
    by_world = {w: list(rng.normal(0.05, 0.01, 20)) for w in range(12)}
    pooled = [v for vals in by_world.values() for v in vals]
    lo, hi = mv.world_bootstrap(by_world, np.median, seed=1)
    assert lo < float(np.median(pooled)) < hi


def test_resampling_worlds_is_wider_than_resampling_observations() -> None:
    """The point of the whole exercise, measured.

    Ten worlds, each with a level of its own and twenty segments around it.
    Treating the 200 segments as independent gives an interval that ignores
    the between-world spread, which is where nearly all the uncertainty is.
    """
    rng = np.random.default_rng(3)
    by_world = {}
    for w in range(10):
        level = rng.normal(0.05, 0.03)  # the world's own draw
        by_world[w] = list(rng.normal(level, 0.002, 20))
    pooled = [v for vals in by_world.values() for v in vals]

    world_lo, world_hi = mv.world_bootstrap(by_world, np.mean, seed=1)
    # The naive alternative: every observation its own "world".
    obs_lo, obs_hi = mv.world_bootstrap({i: [v] for i, v in enumerate(pooled)}, np.mean, seed=1)
    assert (world_hi - world_lo) > 3 * (obs_hi - obs_lo), (
        f"world-level width {world_hi - world_lo:.4f} against observation-level "
        f"{obs_hi - obs_lo:.4f}: the fixture does not have between-world spread, "
        "so the test proves nothing"
    )


def test_the_interval_is_reproducible() -> None:
    by_world = {w: [0.01 * w, 0.02 * w] for w in range(8)}
    a = mv.world_bootstrap(by_world, np.median, seed=5)
    b = mv.world_bootstrap(by_world, np.median, seed=5)
    assert a == b


def test_a_tail_interval_says_whether_it_met_the_precision_goal() -> None:
    tight = mv._interval_words((0.10, 0.12), mv.TAIL_PRECISION_GOAL)
    wide = mv._interval_words((0.10, 0.40), mv.TAIL_PRECISION_GOAL)
    assert "meets" in tight and "misses" in wide
    assert "too few worlds" in mv._interval_words(None)


@pytest.mark.parametrize("stat", [np.median, np.mean, lambda v: np.quantile(v, 0.9)])
def test_every_reported_statistic_can_be_bootstrapped(stat: object) -> None:
    by_world = {w: list(np.linspace(0, 0.1, 10)) for w in range(6)}
    out = mv.world_bootstrap(by_world, stat, n_boot=200, seed=0)
    assert out is not None and out[0] <= out[1]
