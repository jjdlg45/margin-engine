"""Win/loss modeling from quotes (Phase 16; reworked in Run 6, Phase 77).

Per segment: logistic regression of the decision on the quote price index
(quoted price / effective-dated list at the quote date), with controls, with
standard errors clustered where quotes are not independent, and with a
held-out calibration check. Reported with uncertainty and an evidence status:

- insufficient_data: too few decided quotes, no price variation, or outcomes
  separable (a degenerate fit would be meaningless confidence).
- insufficient_precision: the fit converged but either its own relative
  standard error (se / |coefficient|) exceeds the configured maximum, or its
  probabilities are the wrong shape out of sample. Coefficient, SE and CI are
  still reported; the status keeps them out of accuracy gating and win-aware
  optimization (fail closed).
- estimated_win_curve: fitted with finite clustered standard errors at or
  under the relative-SE maximum, and calibrated on held-out quotes (or with
  too few held-out quotes to test, which is said rather than assumed).

Three things a win/loss model has to get right before any of that, and none
of which the Phase 16 implementation did (Run 6, Finding 21):

**Only decided quotes are evidence.** `outcome == "W"` was a win and
everything else was a loss, so an open quote - no decision yet - counted as
the customer saying no, and a quote withdrawn because the customer's project
was cancelled counted as a price rejection. Outcomes are now classified
against configured code lists; open, withdrawn and expired quotes are
excluded with a reason, and a code in none of the lists is excluded as
unrecognized rather than read as a loss.

**One decision is one observation.** Revisions of the same opportunity are
one negotiation, not several independent draws. Where the mapping provides a
quote id, only the latest version of each is modeled and the collapsed
revisions are counted.

**Quotes are not independent.** Standard errors cluster at the opportunity,
customer or rep level, and the number of clusters is reported, because that
- not the row count - is what the precision gate is really reading.

The negotiation gap (quote -> close on won deals) informs the start price:
quoting start = target / (1 - gap) lands at the target after typical
negotiation. Win-aware optimization maximizes the pocket objective x P_win(P)
on a grid - used only where the segment's elasticity is causal-supported AND
the win curve is estimated; everything else stays provisional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from margin_engine.config import Rules

STATUS_INSUFFICIENT = "insufficient_data"
STATUS_LOW_PRECISION = "insufficient_precision"
STATUS_ESTIMATED = "estimated_win_curve"

# Outcome classes. Only the first two are evidence about price.
OUTCOME_WON = "won"
OUTCOME_LOST = "lost"
OUTCOME_OPEN = "open"
OUTCOME_WITHDRAWN = "withdrawn"
OUTCOME_EXPIRED = "expired"
OUTCOME_UNRECOGNIZED = "outcome_not_recognized"
DECIDED = (OUTCOME_WON, OUTCOME_LOST)

#: Why a quote did not reach the model, in the order they are applied.
EXCLUSION_REASONS = (
    OUTCOME_OPEN,
    OUTCOME_WITHDRAWN,
    OUTCOME_EXPIRED,
    OUTCOME_UNRECOGNIZED,
    "superseded_revision",
    "no_list_price",
    "no_segment",
)

#: Plain English for each exclusion, for notes and the report.
_EXCLUSION_WORDS = {
    OUTCOME_OPEN: "no decision yet",
    OUTCOME_WITHDRAWN: "withdrawn before a decision",
    OUTCOME_EXPIRED: "expired without a decision",
    OUTCOME_UNRECOGNIZED: "outcome code not recognized",
    "superseded_revision": "superseded by a later revision",
    "no_list_price": "no dated list price at the quote date",
    "no_segment": "the customer and item are in no commercial segment",
}


def describe_exclusion(reason: str) -> str:
    """Why a quote did not reach the model, in words a reader can use."""
    return _EXCLUSION_WORDS.get(reason, reason.replace("_", " "))


CALIBRATION_OK = "calibrated"
CALIBRATION_OFF = "miscalibrated"
CALIBRATION_UNTESTED = "not testable"


@dataclass
class QuoteIntake:
    """What came in, what was modeled, and why the rest was not."""

    n_rows: int = 0
    unit: str = "line"
    n_modeled: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "unit": self.unit,
            "n_modeled": self.n_modeled,
            "excluded": dict(self.excluded),
            "notes": list(self.notes),
        }


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
    # Run 6, Phase 77.
    n_won: int = 0
    n_lost: int = 0
    cluster_level: str = "none"
    n_clusters: int | None = None
    controls: str = ""
    holdout_n: int = 0
    calibration_slope: float | None = None
    calibration_intercept: float | None = None
    brier: float | None = None
    auc: float | None = None
    calibration_status: str = CALIBRATION_UNTESTED

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
    )


def classify_outcomes(quotes: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Map source outcome codes to outcome classes, case-insensitively.

    A code that matches no configured list becomes `outcome_not_recognized`.
    That is the fail-closed answer: an unknown code is a mapping question, and
    guessing it is a loss manufactures evidence that the price was too high.
    """
    sc = rules.sensitivity
    buckets = (
        (OUTCOME_WON, sc.quote_outcome_won),
        (OUTCOME_LOST, sc.quote_outcome_lost),
        (OUTCOME_OPEN, sc.quote_outcome_open),
        (OUTCOME_WITHDRAWN, sc.quote_outcome_withdrawn),
        (OUTCOME_EXPIRED, sc.quote_outcome_expired),
    )
    code = pl.col("outcome").cast(pl.String).str.strip_chars().str.to_uppercase()
    expr: pl.Expr = pl.lit(OUTCOME_UNRECOGNIZED)
    # Built back to front so the first configured list wins a code that
    # appears in two of them.
    for name, codes in reversed(buckets):
        upper = [c.upper() for c in codes]
        if upper:
            expr = pl.when(code.is_in(upper)).then(pl.lit(name)).otherwise(expr)
    return quotes.with_columns(expr.alias("outcome_class"))


def _resolve_unit(quotes: pl.DataFrame, rules: Rules) -> str:
    want = rules.sensitivity.quote_unit
    has_id = "quote_id" in quotes.columns
    if want == "opportunity":
        if not has_id:
            msg = (
                "sensitivity.quote_unit is 'opportunity' but the quotes mapping "
                "declares no quote_id column, so opportunities cannot be "
                "identified. Map quote_id, or set quote_unit to 'line'."
            )
            raise ValueError(msg)
        return "opportunity"
    if want == "line":
        return "line"
    return "opportunity" if has_id else "line"


def collapse_revisions(quotes: pl.DataFrame, unit: str) -> tuple[pl.DataFrame, int]:
    """Keep one row per opportunity: its latest revision.

    A quote revised three times is one negotiation with one outcome. Counting
    each revision separately triples a deal's weight and - because revisions
    usually walk the price down - tilts the fitted curve toward the prices
    that needed revising.

    Ties on version break on the latest quote date, then on the source order,
    so the choice is reproducible.
    """
    if unit != "opportunity" or "quote_id" not in quotes.columns:
        return quotes, 0
    sort_cols = ["quote_id"]
    descending = [False]
    if "quote_version" in quotes.columns:
        sort_cols.append("quote_version")
        descending.append(False)
    if "quote_date" in quotes.columns:
        sort_cols.append("quote_date")
        descending.append(False)
    ordered = quotes.sort(sort_cols, descending=descending, maintain_order=True)
    latest = ordered.group_by("quote_id", maintain_order=True).last()
    return latest, int(quotes.height - latest.height)


def prepare_quotes(
    quotes_indexed: pl.DataFrame, segment_of_pair: pl.DataFrame, rules: Rules
) -> tuple[pl.DataFrame, QuoteIntake]:
    """Everything between the quotes table and the model, counted.

    Returns the rows a win/loss model may use and a record of what was left
    out and why. Nothing is dropped silently.
    """
    intake = QuoteIntake(n_rows=int(quotes_indexed.height))
    if quotes_indexed.height == 0:
        intake.notes.append("the quotes table has no rows with a dated list price")
        return quotes_indexed, intake
    if "outcome" not in quotes_indexed.columns:
        # An optional module switches off and says why; it never invents the
        # decision it exists to model.
        intake.notes.append(
            "the quotes mapping declares no outcome column, so no quote can be "
            "classified as won or lost and win/loss modeling is unavailable"
        )
        return quotes_indexed.clear(), intake

    work = classify_outcomes(quotes_indexed, rules)
    unit = _resolve_unit(work, rules)
    intake.unit = unit
    if unit == "line":
        intake.notes.append(
            "the quotes mapping declares no quote id, so one quote line is one "
            "observation; revisions of the same negotiation cannot be collapsed"
        )

    work, n_revisions = collapse_revisions(work, unit)
    if n_revisions:
        intake.excluded["superseded_revision"] = n_revisions
        intake.notes.append(
            f"{n_revisions} superseded revisions collapsed into the latest "
            "version of their opportunity"
        )

    counts = dict(work.group_by("outcome_class").len().iter_rows())
    for cls in (OUTCOME_OPEN, OUTCOME_WITHDRAWN, OUTCOME_EXPIRED, OUTCOME_UNRECOGNIZED):
        if counts.get(cls):
            intake.excluded[cls] = int(counts[cls])
    if intake.excluded.get(OUTCOME_UNRECOGNIZED):
        codes = (
            work.filter(pl.col("outcome_class") == OUTCOME_UNRECOGNIZED)["outcome"]
            .unique()
            .sort()
            .to_list()
        )
        intake.notes.append(
            f"outcome codes {codes} match no configured list and were excluded; "
            "add them to sensitivity.quote_outcome_* so their meaning is declared"
        )

    work = work.filter(pl.col("outcome_class").is_in(list(DECIDED))).with_columns(
        (pl.col("outcome_class") == OUTCOME_WON).alias("won")
    )
    before = work.height
    work = work.join(segment_of_pair, on=["customer_id", "item_id"], how="left")
    work = work.filter(pl.col("segment_leaf").is_not_null())
    if before - work.height:
        intake.excluded["no_segment"] = int(before - work.height)
    intake.n_modeled = int(work.height)
    return work, intake


def _design(grp: pl.DataFrame, controls: tuple[str, ...]) -> tuple[np.ndarray, list[str]]:
    """Price index first, then whichever controls the data supports."""
    cols = [grp["quote_index"].to_numpy().astype(float)]
    names = ["quote_index"]
    for c in controls:
        if c == "time" and "quote_date" in grp.columns:
            days = grp["quote_date"].cast(pl.Date).to_numpy().astype("datetime64[D]").astype(int)
            span = float(days.max() - days.min())
            if span > 0:
                cols.append((days - days.min()) / span)
                names.append("time")
        elif c == "log_quantity" and "quantity" in grp.columns:
            q = grp["quantity"].cast(pl.Float64).fill_null(0.0).to_numpy()
            if np.nanstd(q) > 0:
                cols.append(np.log1p(np.clip(q, 0, None)))
                names.append("log_quantity")
    return np.column_stack(cols), names


def _cluster_groups(grp: pl.DataFrame, level: str) -> tuple[np.ndarray | None, str, int | None]:
    """Resolve the clustering level to an integer group code per row."""
    order = (
        [("quote", "quote_id"), ("customer", "customer_id"), ("rep", "sales_rep_id")]
        if level == "auto"
        else [
            (level, {"quote": "quote_id", "customer": "customer_id", "rep": "sales_rep_id"}[level])
        ]
        if level != "none"
        else []
    )
    for name, col in order:
        if col in grp.columns and grp[col].null_count() < grp.height:
            codes = grp[col].cast(pl.String).fill_null("unknown").cast(pl.Categorical)
            arr = codes.to_physical().to_numpy()
            return arr, name, len(np.unique(arr))
    return None, "none", None


def _calibration(y: np.ndarray, p: np.ndarray) -> tuple[float | None, float | None, float, float]:
    """Calibration slope and intercept, Brier score and the c-statistic.

    The slope comes from a logistic regression of the held-out outcome on the
    fitted log-odds. A slope of 1 means the probabilities are the right shape;
    below 1 means the curve is too confident, above 1 too timid.
    """
    from statsmodels.discrete.discrete_model import Logit
    from statsmodels.tools import add_constant

    eps = 1e-6
    p = np.clip(p, eps, 1 - eps)
    brier = float(np.mean((p - y) ** 2))
    pos, neg = y == 1, y == 0
    if pos.any() and neg.any():
        ranks = np.argsort(np.argsort(p)) + 1
        n1, n0 = int(pos.sum()), int(neg.sum())
        auc = float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))
    else:
        auc = float("nan")
    logit = np.log(p / (1 - p))
    if not (pos.any() and neg.any()) or float(np.std(logit)) < 1e-9:
        return None, None, brier, auc
    try:
        fit = Logit(y, add_constant(logit)).fit(disp=0)
        return float(fit.params[1]), float(fit.params[0]), brier, auc
    except Exception:
        return None, None, brier, auc


def fit_win_curves(
    quotes_indexed: pl.DataFrame,
    segment_of_pair: pl.DataFrame,
    rules: Rules,
) -> tuple[pl.DataFrame, QuoteIntake]:
    """One logistic fit per segment, on decided quotes only.

    Segments with fewer than `min_quotes_per_segment` decided quotes are
    insufficient_data; fits whose clustered relative standard error exceeds
    `win_curve_max_rel_se`, or whose held-out calibration slope falls outside
    `win_curve_calibration_band`, are insufficient_precision. Both are
    reported in full and neither is gated on for accuracy.
    """
    # Narrow import: statsmodels.api pulls the whole tsa package, whose
    # compiled extensions a locked-down host (Smart App Control) can block.
    # Logit is all this module needs.
    from statsmodels.discrete.discrete_model import Logit
    from statsmodels.tools import add_constant

    sc = rules.sensitivity
    q, intake = prepare_quotes(quotes_indexed, segment_of_pair, rules)
    out: list[dict[str, object]] = []
    if q.height == 0:
        return pl.DataFrame(), intake

    for (seg,), grp in sorted(q.group_by(["segment_leaf"]), key=lambda kv: kv[0]):
        grp = grp.sort("quote_date") if "quote_date" in grp.columns else grp
        wc = WinCurve(segment=int(seg), n_quotes=grp.height)
        wc.n_won = int(grp["won"].sum())
        wc.n_lost = int(grp.height - wc.n_won)
        wc.win_rate = float(grp["won"].mean() or 0.0)  # type: ignore[arg-type]
        gap_rows = grp.filter(pl.col("won") & pl.col("close_price_cents").is_not_null())
        if gap_rows.height:
            wc.negotiation_gap = float(
                gap_rows.select(
                    (1 - pl.col("close_price_cents") / pl.col("quoted_price_cents")).mean()
                ).item()
            )
        if grp.height < sc.min_quotes_per_segment:
            wc.reasons = f"only {grp.height} decided quotes"
            out.append(wc.to_dict())
            continue
        x, names = _design(grp, sc.win_curve_controls)
        y = grp["won"].to_numpy().astype(float)
        wc.controls = ", ".join(names[1:]) or "none"
        if float(np.std(x[:, 0])) < 1e-4 or wc.win_rate in (0.0, 1.0):
            wc.reasons = "no price variation or degenerate outcomes"
            out.append(wc.to_dict())
            continue
        groups, level, n_clusters = _cluster_groups(grp, sc.win_curve_cluster)
        wc.cluster_level, wc.n_clusters = level, n_clusters
        try:
            model = Logit(y, add_constant(x))
            fit = (
                model.fit(disp=0, cov_type="cluster", cov_kwds={"groups": groups})
                if groups is not None
                else model.fit(disp=0)
            )
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
            _holdout(wc, grp, x, y, sc)
            rel_se = se / abs(wc.price_coef) if wc.price_coef else float("inf")
            if rel_se > sc.win_curve_max_rel_se:
                wc.status = STATUS_LOW_PRECISION
                wc.reasons = (
                    f"relative SE {rel_se:.3f} > {sc.win_curve_max_rel_se} "
                    f"({level} clustering, {n_clusters} groups): the fit does not "
                    "support estimated-curve accuracy; CI reported, not gated"
                )
            elif wc.calibration_status == CALIBRATION_OFF:
                wc.status = STATUS_LOW_PRECISION
                wc.reasons = (
                    f"held-out calibration slope {wc.calibration_slope:.2f} is outside "
                    f"{sc.win_curve_calibration_band}: the probabilities are the wrong "
                    "shape out of sample however tight the standard error is"
                )
            else:
                wc.status = STATUS_ESTIMATED
        except Exception as exc:  # fit failure is a status, not a crash
            wc.reasons = f"fit failed: {exc}"
        out.append(wc.to_dict())
    return pl.DataFrame(out), intake


def _holdout(wc: WinCurve, grp: pl.DataFrame, x: np.ndarray, y: np.ndarray, sc: Any) -> None:
    """Refit on the earlier quotes and score the later ones.

    In sample a logistic fit is calibrated by construction, so an in-sample
    check would always pass. The split is by time rather than at random
    because that is the use: a curve fitted on what has happened is applied to
    quotes that have not been decided yet.
    """
    from statsmodels.discrete.discrete_model import Logit
    from statsmodels.tools import add_constant

    n = len(y)
    n_hold = round(n * sc.win_curve_holdout_share)
    if n_hold < sc.win_curve_min_holdout:
        wc.calibration_status = CALIBRATION_UNTESTED
        return
    cut = n - n_hold
    y_fit, y_hold = y[:cut], y[cut:]
    if len(np.unique(y_fit)) < 2 or len(np.unique(y_hold)) < 2:
        wc.calibration_status = CALIBRATION_UNTESTED
        return
    try:
        sub = Logit(y_fit, add_constant(x[:cut])).fit(disp=0)
        p_hold = np.asarray(sub.predict(add_constant(x[cut:], has_constant="add")), dtype=float)
    except Exception:
        wc.calibration_status = CALIBRATION_UNTESTED
        return
    slope, intercept, brier, auc = _calibration(y_hold, p_hold)
    wc.holdout_n = int(n_hold)
    wc.calibration_slope = slope
    wc.calibration_intercept = intercept
    wc.brier = brier
    wc.auc = None if np.isnan(auc) else auc
    lo, hi = sc.win_curve_calibration_band
    if slope is None:
        wc.calibration_status = CALIBRATION_UNTESTED
    else:
        wc.calibration_status = CALIBRATION_OK if lo <= slope <= hi else CALIBRATION_OFF


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
