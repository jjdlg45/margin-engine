"""Pooled event study for implemented price actions (Run 6, Phase 75).

The per-action figure in `tracking.py` is a control-adjusted volume change: one
treated pair's ratio divided by the median ratio of untreated pairs in its
segment. It removes the general trend and claims nothing more. This module is
the formal counterpart - one estimate pooled across every implemented action,
with the assumptions written down.

**The estimand.** The average proportional change in monthly quantity for
treated customer-item pairs over the periods after their price change,
relative to what those same pairs would have bought had the price not changed,
with the counterfactual taken from pairs that are not treated in that period.
It is an average over the treated pairs that actually appear, not over a
population: pairs with no pre-period and segments with no controls are absent
from it, and their counts are reported.

**What makes it a staggered design.** Actions are implemented on different
dates, so the panel has cohorts. The control group for a given period is every
pair that is **never treated** plus every pair **not yet treated** in that
period. An already-treated pair is never used as a control for a later one -
the comparison that makes two-way fixed-effects estimates on staggered
adoption go wrong, sometimes in sign, as set out in Baker, Callaway,
Cunningham, Goodman-Bacon and Sant'Anna's practitioner's guide (Journal of
Economic Literature). This is an event-study specification with that
restriction imposed, estimated by Poisson pseudo-likelihood; it is not the
Callaway-Sant'Anna group-time estimator, and the report does not call it one.

**Why Poisson.** The outcome includes zeros - a pair that stopped buying is
the result that matters most here, and logs cannot represent it. Poisson
pseudo-likelihood takes the level outcome, handles the zeros natively, and
gives coefficients that read as proportional effects.

**Pre-trends.** The lead coefficients are reported, never summarised into a
pass. A lead near zero with a wide interval is a test that could not see
anything, and it is labelled that way: the reported status is a failure when a
lead is materially non-zero, and otherwise says what size of pre-trend the
data could have detected. Parallel trends is an assumption; no test here
establishes it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from margin_engine.config import Rules

STATUS_ESTIMATED = "estimated"
STATUS_INSUFFICIENT = "insufficient_evidence"

PRETREND_CLEAN = "no_pretrend_detected"
PRETREND_FAILED = "pretrend_detected"
PRETREND_UNTESTABLE = "pretrend_not_testable"


@dataclass
class EventStudy:
    status: str = STATUS_INSUFFICIENT
    reason: str = ""
    # Average post-treatment effect, as a proportional change in quantity.
    att_pct: float | None = None
    att_se: float | None = None
    att_ci_low: float | None = None
    att_ci_high: float | None = None
    # Event-time coefficients, negative k = before treatment.
    coefficients: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema=COEF_SCHEMA))
    pretrend_status: str = PRETREND_UNTESTABLE
    pretrend_detail: str = ""
    n_treated_pairs: int = 0
    n_control_pairs: int = 0
    n_cohorts: int = 0
    n_rows: int = 0
    estimand: str = ""


COEF_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "event_time": pl.Int64,
    "coefficient_pct": pl.Float64,
    "se_pct": pl.Float64,
    "ci_low_pct": pl.Float64,
    "ci_high_pct": pl.Float64,
    "n_treated_pairs": pl.Int64,
    "period": pl.String,
}

ESTIMAND = (
    "average proportional change in monthly quantity for treated customer-item "
    "pairs after their price change, against pairs not treated in the same period "
    "(never-treated and not-yet-treated), with pair and calendar-month fixed "
    "effects"
)


def _month_index(d: dt.date) -> int:
    return d.year * 12 + (d.month - 1)


def build_event_panel(
    sales: pl.DataFrame,
    treatments: pl.DataFrame,
    control_pairs: pl.DataFrame,
    *,
    leads: int,
    lags: int,
) -> pl.DataFrame:
    """A balanced pair x month panel of quantities around each treatment.

    `treatments` carries customer_id, item_id, implemented_on. `control_pairs`
    carries the pairs eligible to serve as controls. Months with no invoice are
    real zeros here - the pair existed and bought nothing - which is why the
    panel is built by cross join rather than from the invoices alone.
    """
    if not treatments.height:
        return pl.DataFrame(
            schema={
                "customer_id": pl.String,
                "item_id": pl.String,
                "month_idx": pl.Int64,
                "qty": pl.Float64,
                "treat_month": pl.Int64,
                "event_time": pl.Int64,
                "treated": pl.Boolean,
            }
        )
    monthly = (
        sales.with_columns(
            (pl.col("invoice_date").dt.year() * 12 + pl.col("invoice_date").dt.month() - 1)
            .cast(pl.Int64)
            .alias("month_idx")
        )
        .group_by("customer_id", "item_id", "month_idx")
        .agg(pl.col("quantity_norm").sum().alias("qty"))
    )
    lo = int(monthly["month_idx"].min())  # type: ignore[arg-type]
    hi = int(monthly["month_idx"].max())  # type: ignore[arg-type]
    treat = treatments.with_columns(
        (pl.col("implemented_on").dt.year() * 12 + pl.col("implemented_on").dt.month() - 1)
        .cast(pl.Int64)
        .alias("treat_month")
    ).select("customer_id", "item_id", "treat_month")
    pairs = (
        pl.concat(
            [
                treat.select("customer_id", "item_id"),
                control_pairs.select("customer_id", "item_id"),
            ]
        )
        .unique()
        .sort("customer_id", "item_id")
    )
    months = pl.DataFrame({"month_idx": list(range(lo, hi + 1))})
    panel = (
        pairs.join(months, how="cross")
        .join(monthly, on=["customer_id", "item_id", "month_idx"], how="left")
        .with_columns(pl.col("qty").fill_null(0.0))
        .join(treat, on=["customer_id", "item_id"], how="left")
    )
    # Event time is only defined for treated pairs; controls keep a null so
    # they contribute to the fixed effects and to the never-treated baseline
    # without ever entering an event-time dummy.
    panel = panel.with_columns(
        pl.when(pl.col("treat_month").is_not_null())
        .then(pl.col("month_idx") - pl.col("treat_month"))
        .otherwise(None)
        .alias("event_time"),
        pl.col("treat_month").is_not_null().alias("treated"),
    )
    # Keep the window around each treatment, and every row of a never-treated
    # pair. A treated pair's rows outside its own window are dropped rather
    # than pooled into the baseline, which is what keeps an already-treated
    # pair out of anyone else's control group.
    return panel.filter(
        pl.col("event_time").is_null()
        | ((pl.col("event_time") >= -leads) & (pl.col("event_time") <= lags))
    )


def _ppml_event_study(
    panel: pl.DataFrame, *, leads: int, lags: int
) -> tuple[dict[int, tuple[float, float]], str] | None:
    """Fit the event study by Poisson pseudo-likelihood with pair and
    calendar-month fixed effects. Returns {event_time: (coef, se)}.

    Event time -1 is the omitted base period, so every coefficient reads as a
    change against the month before the price moved.
    """
    import pandas as pd

    present = sorted(
        int(k)
        for k in panel.filter(pl.col("event_time").is_not_null())["event_time"].unique().to_list()
    )
    terms = [k for k in present if k != -1 and -leads <= k <= lags]
    if not terms:
        return None
    df = pd.DataFrame(
        {
            "qty": panel["qty"].to_numpy(),
            "pair": panel["customer_id"].to_list(),
            "month": panel["month_idx"].to_numpy(),
        }
    )
    df["pair"] = [f"{c}|{i}" for c, i in zip(panel["customer_id"], panel["item_id"], strict=True)]
    ev = panel["event_time"].to_numpy()
    names = []
    for k in terms:
        name = f"d_{'m' if k < 0 else 'p'}{abs(k)}"
        df[name] = np.where(np.equal(ev, k), 1.0, 0.0)
        names.append(name)
    try:
        import pyfixest as pf

        fit = pf.fepois(f"qty ~ {' + '.join(names)} | pair + month", data=df, vcov={"CRV1": "pair"})
        coefs = fit.coef().to_dict()
        ses = fit.se().to_dict()
    except Exception as exc:
        # Any fit failure is a refusal, not a fallback: a Poisson that will not
        # converge has nothing to say about the effect.
        return {}, f"the Poisson fit did not converge: {exc}"
    out: dict[int, tuple[float, float]] = {}
    for k, name in zip(terms, names, strict=True):
        if name in coefs:
            out[k] = (float(coefs[name]), float(ses.get(name, float("nan"))))
    return out, ""


def run_event_study(
    sales: pl.DataFrame,
    treatments: pl.DataFrame,
    control_pairs: pl.DataFrame,
    rules: Rules,
) -> EventStudy:
    """Pooled event study across every implemented price action.

    Refuses rather than guesses: too few treated pairs, too few controls, no
    cohort structure, or a fit that will not converge all produce
    `insufficient_evidence` with the reason.
    """
    ac = rules.actions
    res = EventStudy(estimand=ESTIMAND)
    n_treated = treatments.select("customer_id", "item_id").unique().height
    n_control = (
        control_pairs.select("customer_id", "item_id")
        .unique()
        .join(
            treatments.select("customer_id", "item_id").unique(),
            on=["customer_id", "item_id"],
            how="anti",
        )
        .height
    )
    res.n_treated_pairs = n_treated
    res.n_control_pairs = n_control
    if n_treated < ac.event_study_min_treated_pairs:
        res.reason = (
            f"{n_treated} treated pair(s) against the {ac.event_study_min_treated_pairs} "
            "this estimate needs; the per-action control-adjusted changes are still "
            "reported"
        )
        return res
    if n_control < ac.event_study_min_control_pairs:
        res.reason = (
            f"{n_control} untreated pair(s) against the {ac.event_study_min_control_pairs} "
            "this estimate needs: there is no counterfactual to estimate against"
        )
        return res

    panel = build_event_panel(
        sales,
        treatments,
        control_pairs,
        leads=ac.event_study_leads,
        lags=ac.event_study_lags,
    )
    res.n_rows = panel.height
    res.n_cohorts = int(
        treatments.with_columns(
            (pl.col("implemented_on").dt.year() * 12 + pl.col("implemented_on").dt.month())
            .cast(pl.Int64)
            .alias("c")
        )["c"].n_unique()
    )
    if not panel.height:
        res.reason = "no usable pair-month rows around the implementation dates"
        return res

    fitted = _ppml_event_study(panel, leads=ac.event_study_leads, lags=ac.event_study_lags)
    if fitted is None:
        res.reason = "no event-time periods survive around the implementation dates"
        return res
    coefs, failure = fitted
    if failure or not coefs:
        res.reason = failure or "the Poisson fit produced no event-time coefficients"
        return res

    counts = (
        panel.filter(pl.col("event_time").is_not_null())
        .group_by("event_time")
        .agg(pl.struct("customer_id", "item_id").n_unique().alias("n"))
    )
    count_of = {int(r["event_time"]): int(r["n"]) for r in counts.iter_rows(named=True)}
    rows: list[dict[str, Any]] = []
    for k in sorted(coefs):
        coef, se = coefs[k]
        rows.append(
            {
                "event_time": k,
                "coefficient_pct": float(np.expm1(coef)),
                "se_pct": float(se * np.exp(coef)) if np.isfinite(se) else None,
                "ci_low_pct": float(np.expm1(coef - 1.96 * se)) if np.isfinite(se) else None,
                "ci_high_pct": float(np.expm1(coef + 1.96 * se)) if np.isfinite(se) else None,
                "n_treated_pairs": count_of.get(k, 0),
                "period": "before the change" if k < 0 else "after the change",
            }
        )
    # The omitted base period, shown so the chart has an anchor and nobody
    # reads its absence as a gap in the data.
    rows.append(
        {
            "event_time": -1,
            "coefficient_pct": 0.0,
            "se_pct": None,
            "ci_low_pct": None,
            "ci_high_pct": None,
            "n_treated_pairs": count_of.get(-1, 0),
            "period": "the month before the change (the comparison point)",
        }
    )
    res.coefficients = pl.DataFrame(rows, schema=COEF_SCHEMA).sort("event_time")

    post = [(k, c, s) for k, (c, s) in coefs.items() if k >= 0]
    if post:
        # Equal-weighted average of the post coefficients, with the standard
        # error taken as if they were independent - stated, because they are
        # not, and the interval is therefore optimistic.
        mean_coef = float(np.mean([c for _k, c, _s in post]))
        ses = [s for _k, _c, s in post if np.isfinite(s)]
        mean_se = float(np.sqrt(np.sum(np.square(ses))) / len(post)) if ses else float("nan")
        res.att_pct = float(np.expm1(mean_coef))
        if np.isfinite(mean_se):
            res.att_se = float(mean_se * np.exp(mean_coef))
            res.att_ci_low = float(np.expm1(mean_coef - 1.96 * mean_se))
            res.att_ci_high = float(np.expm1(mean_coef + 1.96 * mean_se))

    pre = [(k, c, s) for k, (c, s) in coefs.items() if k < -1]
    if not pre:
        res.pretrend_status = PRETREND_UNTESTABLE
        res.pretrend_detail = (
            "only one period before the change is in the window, so there is no "
            "pre-trend to look at - which is not the same as there being none"
        )
    else:
        worst_k, worst_c, worst_se = max(pre, key=lambda t: abs(t[1]))
        worst_pct = float(np.expm1(worst_c))
        if abs(worst_pct) > ac.event_study_max_pretrend:
            res.pretrend_status = PRETREND_FAILED
            res.pretrend_detail = (
                f"treated pairs were already moving before the price did: "
                f"{worst_pct:+.1%} at {abs(worst_k)} month(s) before, against a "
                f"{ac.event_study_max_pretrend:.0%} tolerance. The post-change "
                "numbers below are not an effect of the price change."
            )
        else:
            # What size of pre-trend could this data have seen? Reporting the
            # detectable size is the honest version of "no pre-trend found".
            detect = float(np.expm1(1.96 * worst_se)) if np.isfinite(worst_se) else float("nan")
            res.pretrend_status = PRETREND_CLEAN
            res.pretrend_detail = (
                f"the largest movement before the change is {worst_pct:+.1%}, inside the "
                f"{ac.event_study_max_pretrend:.0%} tolerance"
                + (
                    f"; this data could have detected a pre-trend of about "
                    f"{detect:.1%}, so a smaller one would not have shown up"
                    if np.isfinite(detect)
                    else ""
                )
                + ". Parallel trends remains an assumption, not something this check "
                "establishes."
            )

    res.status = STATUS_ESTIMATED
    res.reason = (
        f"{n_treated} treated pair(s) in {res.n_cohorts} implementation wave(s) against "
        f"{n_control} never-treated or not-yet-treated pair(s), {res.n_rows} pair-months"
    )
    return res
