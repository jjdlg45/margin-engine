"""Win/loss modeling from quotes (Phase 16).

Per segment: logistic regression of outcome on the quote price index
(quoted price / effective-dated list at the quote date). Reported with
uncertainty and an evidence status:

- insufficient_data: too few quotes, no price variation, or outcomes separable
  (a degenerate fit would be meaningless confidence).
- insufficient_precision: the fit converged but its own relative standard
  error (se / |coefficient|) exceeds the configured maximum, so the data does
  not support the accuracy an estimated curve claims. Coefficient, SE and CI
  are still reported; the status keeps them out of accuracy gating and
  win-aware optimization (fail closed).
- estimated_win_curve: fitted with finite standard errors at or under the
  relative-SE maximum.

The negotiation gap (quote -> close on won deals) informs the start price:
quoting start = target / (1 - gap) lands at the target after typical
negotiation. Win-aware optimization maximizes (P - C) x Q(P) x P_win(P) on a
grid — used only where the segment's elasticity is causal-supported AND the
win curve is estimated; everything else stays provisional.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

STATUS_INSUFFICIENT = "insufficient_data"
STATUS_LOW_PRECISION = "insufficient_precision"
STATUS_ESTIMATED = "estimated_win_curve"


@dataclass
class WinCurve:
    segment: int
    n_quotes: int
    intercept: float | None = None
    price_coef: float | None = None
    price_coef_se: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    win_rate: float = 0.0
    negotiation_gap: float | None = None
    status: str = STATUS_INSUFFICIENT
    reasons: str = ""

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def quote_price_index(quotes: pl.DataFrame, price_list: pl.DataFrame) -> pl.DataFrame:
    """Join the effective-dated list at the quote date; index = quote / list."""
    pl_sorted = price_list.sort("item_id", "effective_date")
    q = quotes.sort("item_id", "quote_date").join_asof(
        pl_sorted.select("item_id", "effective_date", "list_price_scaled"),
        left_on="quote_date",
        right_on="effective_date",
        by="item_id",
        strategy="backward",
        check_sortedness=False,
    )
    return q.filter(pl.col("list_price_scaled").is_not_null()).with_columns(
        (pl.col("quoted_price_cents") * 100 / pl.col("list_price_scaled")).alias("quote_index"),
        (pl.col("outcome") == "W").alias("won"),
    )


def fit_win_curves(
    quotes_indexed: pl.DataFrame,
    segment_of_pair: pl.DataFrame,
    min_quotes: int = 400,
    max_rel_se: float = 0.10,
) -> pl.DataFrame:
    """One logistic fit per segment. segment_of_pair: customer_id, item_id,
    segment_leaf. Segments with fewer than min_quotes quotes are
    insufficient_data; fits whose relative standard error exceeds max_rel_se
    are insufficient_precision (both reported, never gated on accuracy)."""
    # Narrow import: statsmodels.api pulls the whole tsa package, whose
    # compiled extensions a locked-down host (Smart App Control) can block.
    # Logit is all this module needs.
    from statsmodels.discrete.discrete_model import Logit
    from statsmodels.tools import add_constant

    q = quotes_indexed.join(segment_of_pair, on=["customer_id", "item_id"], how="left").filter(
        pl.col("segment_leaf").is_not_null()
    )
    out: list[dict[str, object]] = []
    for (seg,), grp in sorted(q.group_by(["segment_leaf"]), key=lambda kv: kv[0]):
        wc = WinCurve(segment=int(seg), n_quotes=grp.height)
        wc.win_rate = float(grp["won"].mean() or 0.0)  # type: ignore[arg-type]
        gap_rows = grp.filter(pl.col("won") & pl.col("close_price_cents").is_not_null())
        if gap_rows.height:
            wc.negotiation_gap = float(
                gap_rows.select(
                    (1 - pl.col("close_price_cents") / pl.col("quoted_price_cents")).mean()
                ).item()
            )
        if grp.height < min_quotes:
            wc.reasons = f"only {grp.height} quotes"
            out.append(wc.to_dict())
            continue
        x = grp["quote_index"].to_numpy()
        y = grp["won"].to_numpy().astype(float)
        if float(np.std(x)) < 1e-4 or wc.win_rate in (0.0, 1.0):
            wc.reasons = "no price variation or degenerate outcomes"
            out.append(wc.to_dict())
            continue
        try:
            fit = Logit(y, add_constant(x)).fit(disp=0)
            se = float(fit.bse[1])
            if not np.isfinite(se) or se > 1e3:
                wc.reasons = "separation: standard errors unbounded"
                out.append(wc.to_dict())
                continue
            wc.intercept = float(fit.params[0])
            wc.price_coef = float(fit.params[1])
            wc.price_coef_se = se
            wc.ci_low = wc.price_coef - 1.96 * se
            wc.ci_high = wc.price_coef + 1.96 * se
            rel_se = se / abs(wc.price_coef) if wc.price_coef else float("inf")
            if rel_se > max_rel_se:
                wc.status = STATUS_LOW_PRECISION
                wc.reasons = (
                    f"relative SE {rel_se:.3f} > {max_rel_se}: the fit does not "
                    "support estimated-curve accuracy; CI reported, not gated"
                )
            else:
                wc.status = STATUS_ESTIMATED
        except Exception as exc:  # fit failure is a status, not a crash
            wc.reasons = f"fit failed: {exc}"
        out.append(wc.to_dict())
    return pl.DataFrame(out)


def optimal_price_win_aware(
    *,
    cost_cents: int,
    p0_cents: int,
    win_intercept: float,
    win_price_coef: float,
    list_cents: int,
    floor_cents: int,
    upper_index: float = 1.2,
    grid_step: float = 0.002,
    deduction_bp: int = 0,
    fixed_deduction_cents: int = 0,
    k_per_unit_cents: int | None = None,
    objective: str = "pocket_gross_profit",
) -> tuple[int, float]:
    """argmax over a price-index grid of the POCKET objective x P_win(P),
    clipped to [floor, upper]. Returns (price_cents, win probability there).

    The quote index is translated to a monetary price and the pair's
    post-invoice economics apply (Run 6, Phase 65): unit economics are
    P x (1 - d) - F - C, and under the contribution objective also - K.
    A contribution call without a supported K raises - the engine never
    optimizes against a number it does not have.

    ONE volume-response source per record (Run 4, 24.13): the win curve is the
    only volume response here — deal size is held at baseline. The demand
    elasticity path lives in opportunity.optimize_targets. The two must never
    be multiplied: an elasticity estimated from realized transactions (PPML on
    the panel including zeros) already contains the extensive margin that the
    win curve models — lost deals ARE the zeros — so combining them counts the
    same volume response twice. This function's signature makes the
    combination impossible rather than merely discouraged.
    """
    del p0_cents  # deal size held at baseline; kept for call-site clarity
    if objective not in ("pocket_gross_profit", "contribution"):
        msg = f"unknown win-aware objective {objective!r}"
        raise ValueError(msg)
    k = 0
    if objective == "contribution":
        if k_per_unit_cents is None:
            msg = (
                "contribution objective needs the pair's per-unit cost-to-serve K; "
                "none is supported for this pair, so the contribution optimum is "
                "unavailable (use the pocket_gross_profit objective instead)"
            )
            raise ValueError(msg)
        k = k_per_unit_cents
    retain = (10_000 - deduction_bp) / 10_000
    lo = max(floor_cents / list_cents, 0.01)
    grid = np.arange(lo, upper_index, grid_step)
    prices = grid * list_cents
    p_win = 1.0 / (1.0 + np.exp(-(win_intercept + win_price_coef * grid)))
    unit = prices * retain - fixed_deduction_cents - cost_cents - k
    profit = unit * p_win
    best = int(np.argmax(profit))
    return round(prices[best]), float(p_win[best])
