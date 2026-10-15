"""Largest-remainder (Hamilton) allocation: fixtures and property tests
(phase-8.2, spec 8.2).
"""

from __future__ import annotations

from fractions import Fraction

import polars as pl
from hypothesis import given, settings
from hypothesis import strategies as st

from margin_engine.waterfall.allocate import allocate_exact


def _alloc(weights: list[float], amount: int, keys: list[str] | None = None, **kw) -> list[int]:
    keys = keys or [f"L{i:03d}" for i in range(len(weights))]
    lines = pl.DataFrame(
        {
            "g": ["G"] * len(weights),
            "source_line_key": keys,
            "w": pl.Series(weights, dtype=pl.Int64),
        }
    )
    amounts = pl.DataFrame({"g": ["G"], "amt": pl.Series([amount], dtype=pl.Int64)})
    out = allocate_exact(lines, amounts, "g", "amt", "w", "alloc", **kw)
    return out.sort("source_line_key")["alloc"].to_list()


def test_hamilton_equal_thirds() -> None:
    # $1.00 over three equal lines: 34/33/33; the extra cent goes to the
    # smallest source_line_key (remainders and weights tie).
    assert _alloc([1, 1, 1], 100) == [34, 33, 33]


def test_hamilton_classic_example() -> None:
    # Shares of 100 by weights 5/3/2 are exact: 50/30/20.
    assert _alloc([5, 3, 2], 100) == [50, 30, 20]
    # 101 cents: exact shares 50.5/30.3/20.2 -> floors 50/30/20, one leftover
    # cent to the largest remainder (0.5).
    assert _alloc([5, 3, 2], 101) == [51, 30, 20]


def test_tie_break_larger_weight_then_key() -> None:
    # Weights 2/2/1, amount 103: exact 41.2/41.2/20.6 -> floors 41/41/20,
    # leftover 1 cent; remainders 0.2/0.2/0.6 -> the 0.6 line wins first.
    assert _alloc([2, 2, 1], 103) == [41, 41, 21]
    # Amount 104: two leftover cents; after the 0.6-remainder line, the
    # 0.2/0.2 tie breaks by key (L0 before L1).
    assert _alloc([2, 2, 1], 104) == [42, 41, 21]


def test_signed_weights_returns_carry_negative_share() -> None:
    # Settlement over a sale (+300) and a return (-100): W=200, amount 200
    # -> exact shares +300 and -100.
    assert _alloc([300, -100], 200, signed_driver=True) == [300, -100]
    # Non-divisible: amount 201 -> exact 301.5 / -100.5 -> floors 301 / -101,
    # leftover 1 cent to the larger remainder (both 0.5; |w| 300 > 100).
    assert _alloc([300, -100], 201, signed_driver=True) == [302, -101]


def test_negative_amount_allocates_exactly() -> None:
    out = _alloc([5, 3, 2], -101)
    assert sum(out) == -101
    # Exact shares -50.5/-30.3/-20.2: every line within 1 cent of exact.
    exact = [Fraction(-101 * w, 10) for w in (5, 3, 2)]
    for a, e in zip(out, exact, strict=True):
        assert abs(Fraction(a) - e) < 1


def test_zero_weights_fall_back_to_equal_split() -> None:
    assert _alloc([0, 0, 0], 100) == [34, 33, 33]


@given(
    weights=st.lists(st.integers(min_value=0, max_value=1_000_000), min_size=1, max_size=12),
    amount=st.integers(min_value=-10_000_000, max_value=10_000_000),
)
@settings(max_examples=200, deadline=None)
def test_property_sums_exact_and_within_one_cent(weights: list[int], amount: int) -> None:
    out = _alloc(weights, amount)
    assert sum(out) == amount
    total_w = sum(weights)
    eff = weights if total_w > 0 else [1] * len(weights)
    eff_total = sum(eff)
    for a, w in zip(out, eff, strict=True):
        exact = Fraction(amount * w, eff_total)
        assert abs(Fraction(a) - exact) < 1, (a, exact)


@given(
    weights=st.lists(st.integers(min_value=-500_000, max_value=1_000_000), min_size=1, max_size=12),
    amount=st.integers(min_value=0, max_value=10_000_000),
)
@settings(max_examples=200, deadline=None)
def test_property_signed_weights(weights: list[int], amount: int) -> None:
    out = _alloc(weights, amount, signed_driver=True)
    total_w = sum(weights)
    if total_w == 0:
        # Only an exactly-zero signed total is unallocatable.
        assert all(a == 0 for a in out)
        return
    assert sum(out) == amount
    for a, w in zip(out, weights, strict=True):
        exact = Fraction(amount * w, total_w)
        assert abs(Fraction(a) - exact) < 1, (a, exact)


def test_return_only_group_allocates_fully() -> None:
    """A settlement over a return-only group (all-negative signed weights) is
    allocated, not left as residual: the single return line carries the whole
    (negative) settlement amount."""
    assert _alloc([-10_000], -300, signed_driver=True) == [-300]
    # Two returns split proportionally: exact shares -200 / -100 of -300.
    assert _alloc([-10_000, -5_000], -300, signed_driver=True) == [-200, -100]
    # Exactly offsetting sale and return: no defined shares, unallocatable.
    assert _alloc([5_000, -5_000], -300, signed_driver=True) == [0, 0]
