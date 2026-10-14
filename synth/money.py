"""Exact integer money math for the generator.

All planted amounts are integer cents. Division rounds half-even using pure
integer arithmetic (no float pass), so truth values are exactly reproducible
and reconcile to the cent.
"""

from __future__ import annotations

import numpy as np


def div_round_half_even(numer: np.ndarray, denom: int | np.ndarray) -> np.ndarray:
    """Elementwise round(numer / denom) with banker's rounding, exact integer math.

    Works for signed numerators; denom must be positive.
    """
    numer = np.asarray(numer, dtype=np.int64)
    d = np.asarray(denom, dtype=np.int64)
    if np.any(d <= 0):
        msg = "denominator must be positive"
        raise ValueError(msg)
    # Python-style floor division/mod keep 0 <= r < d for any sign of numer.
    q = numer // d
    r = numer % d
    twice = 2 * r
    round_up = (twice > d) | ((twice == d) & (q % 2 != 0))
    return (q + round_up.astype(np.int64)).astype(np.int64)


def apply_bp(amount_cents: np.ndarray, rate_bp: np.ndarray | int) -> np.ndarray:
    """amount * rate, rate in basis points, half-even to the cent."""
    amount_cents = np.asarray(amount_cents, dtype=np.int64)
    rate = np.asarray(rate_bp, dtype=np.int64)
    return div_round_half_even(amount_cents * rate, 10_000)
