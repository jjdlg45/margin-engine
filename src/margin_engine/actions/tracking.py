"""Results tracking (Phase 41, reworked in Run 6 Phase 75): the feedback loop.

After an action is marked implemented, measure over a configurable window
(default 90 days on each side of the implementation date):

- Price realization: did the invoiced price actually move to the new price, or
  did it erode back? realization = (P_after - P_before) / (P_new - P_before),
  1.0 = fully realized, 0 = no move, negative = eroded further.
- **Control-adjusted volume change**: the treated pair's volume ratio
  (after/before) divided by the same ratio for a comparison group of untreated
  pairs in the same segment. It removes the general trend, and that is all it
  claims. It is NOT called a difference-in-differences estimate, because a
  per-action ratio against a median of untreated ratios does not carry the
  assumptions that name implies. The formal estimate lives in
  `event_study.py`, pooled across actions, and says what it assumes.
- Margin outcome: pocket margin and contribution, before vs after.

**A pair that stops buying is a result, not a missing value.** Earlier versions
refused to measure an action whose pair had too few sales after the change -
which refused exactly the pairs where the price increase did the most. An
empty after window is now a volume ratio of zero with a `stopped_buying` flag,
and the same applies to the controls: a control pair that bought before and
not after contributes a zero, not an absence. The minimum sales gate applies
to the BEFORE window, which is the baseline and does have to be real.

Every other gate still fails closed: a window that has not elapsed, no
baseline, or too few clean control pairs produce `insufficient_evidence` with
the reason - never a number that cannot be supported. Calibration (prediction
error by evidence bucket) appears only once enough actions are measured;
below the configured minimum it says "not enough implemented actions yet".
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import polars as pl

from margin_engine.actions.event_study import ESTIMAND, EventStudy, run_event_study
from margin_engine.actions.model import replay_status
from margin_engine.config import Rules

if TYPE_CHECKING:
    from margin_engine.actions.store import ActionStore

MEASURED = "measured"
INSUFFICIENT = "insufficient_evidence"

OUTCOMES_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "action_id": pl.String,
    "customer_id": pl.String,
    "item_id": pl.String,
    "implemented_on": pl.Date,
    "window_days": pl.Int64,
    "qty_before": pl.Float64,
    "qty_after": pl.Float64,
    "price_before_cents": pl.Int64,
    "price_after_cents": pl.Int64,
    "proposed_price_cents": pl.Int64,
    "realization_pct": pl.Float64,
    "predicted_volume_ratio": pl.Float64,
    "volume_treatment": pl.String,
    "evidence_status": pl.String,
    "treated_volume_ratio": pl.Float64,
    "stopped_buying": pl.Boolean,
    "control_volume_ratio": pl.Float64,
    "n_control_pairs": pl.Int64,
    "n_control_pairs_excluded": pl.Int64,
    "control_exclusions": pl.String,
    "control_adjusted_volume_ratio": pl.Float64,
    "volume_error_log_points": pl.Float64,
    # Reporting dimensions (Run 6, Phase 75): results are broken out by these
    # rather than pooled into one headline that hides where they came from.
    "wave": pl.String,
    "segment_leaf": pl.Int64,
    "action_type": pl.String,
    "implementation_compliance": pl.String,
    "pocket_margin_before": pl.Float64,
    "pocket_margin_after": pl.Float64,
    "contribution_before_cents": pl.Int64,
    "contribution_after_cents": pl.Int64,
    "outcome_status": pl.String,
    "outcome_reason": pl.String,
}

# Evidence buckets for the calibration report (prediction error by evidence).
# EVERY volume treatment must appear here. The lookup below deliberately has no
# default: an unmapped treatment used to fall into `fixed_volume`, so an action
# sized WITH a modeled volume response would land in the bucket whose own
# tooltip says it was sized at unchanged volume, and shift a published
# calibration figure (Run 6, Phase 72).
_BUCKET = {
    "expected_change": "causal_supported",
    "sensitivity_range_only": "iv_estimated",
    "fixed_volume_only": "fixed_volume",
    "outside_price_support": "fixed_volume",
    "cost_unavailable": "fixed_volume",
    "competitor_path_uncontrolled": "fixed_volume",
}


# How faithfully the invoiced price actually moved to the proposed one. An
# action whose price never moved tells you nothing about price response, and
# pooling it with one that did would quietly dilute the measurement.
COMPLIANCE_FULL = "fully_realized"
COMPLIANCE_PARTIAL = "partly_realized"
COMPLIANCE_NONE = "not_realized"
COMPLIANCE_REVERSED = "moved_the_other_way"
COMPLIANCE_UNKNOWN = "not_known"


def _compliance(realization: float | None) -> str:
    if realization is None:
        return COMPLIANCE_UNKNOWN
    if realization < -0.05:
        return COMPLIANCE_REVERSED
    if realization < 0.25:
        return COMPLIANCE_NONE
    if realization < 0.75:
        return COMPLIANCE_PARTIAL
    return COMPLIANCE_FULL


@dataclass
class TrackingResult:
    outcomes: pl.DataFrame
    calibration: pl.DataFrame
    # Measured results split by implementation wave, segment, action type,
    # evidence bucket and how faithfully the price actually moved.
    by_group: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema=BY_GROUP_SCHEMA))
    # The formal pooled estimate, with its own refusals and its pre-trend
    # diagnostic. Separate from the per-action figures above because it rests
    # on assumptions they do not.
    event_study: EventStudy = field(default_factory=EventStudy)
    notes: list[str] = field(default_factory=list)


BY_GROUP_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "dimension": pl.String,
    "value": pl.String,
    "n_measured": pl.Int64,
    "n_stopped_buying": pl.Int64,
    "median_control_adjusted_volume_ratio": pl.Float64,
    "median_realization_pct": pl.Float64,
}


def _window_stats(
    sales: pl.DataFrame, pairs: pl.DataFrame, lo: pl.Expr, hi: pl.Expr
) -> pl.DataFrame:
    """Per-pair stats over one window: qty, avg invoice price, pocket margin,
    contribution. `pairs` restricts to the pairs of interest."""
    w = sales.join(pairs, on=["customer_id", "item_id"], how="semi").filter(
        (pl.col("invoice_date") >= lo) & (pl.col("invoice_date") <= hi)
    )
    return (
        w.group_by("customer_id", "item_id")
        .agg(
            pl.len().alias("n_lines"),
            pl.col("quantity_norm").sum().alias("qty"),
            pl.col("net_amount_cents").sum().alias("net_cents"),
            pl.col("pocket_revenue_cents")
            .filter(pl.col("product_cost_cents").is_not_null())
            .sum()
            .alias("pocket_cents"),
            pl.col("pocket_gp_cents")
            .filter(pl.col("product_cost_cents").is_not_null())
            .sum()
            .alias("pocket_gp_cents"),
            pl.col("contribution_cents").sum().alias("contribution_cents"),
        )
        .with_columns(
            pl.when(pl.col("qty") > 0)
            .then((pl.col("net_cents") / pl.col("qty")).round(0).cast(pl.Int64))
            .otherwise(None)
            .alias("price_cents"),
            pl.when(pl.col("pocket_cents") > 0)
            .then(pl.col("pocket_gp_cents") / pl.col("pocket_cents"))
            .otherwise(None)
            .alias("pocket_margin"),
        )
    )


def track_outcomes(
    actions: pl.DataFrame,
    events: pl.DataFrame,
    ledger: pl.DataFrame,
    rules: Rules,
    pair_segments: pl.DataFrame | None = None,
    substitution_groups: pl.DataFrame | None = None,
) -> TrackingResult:
    """pair_segments (customer_id, item_id, segment_leaf) widens the
    comparison-group pool to every segmented pair (normally the guidance
    table); without it, only pairs present in the actions table can serve as
    controls - stated in the per-action reason when that pool is too thin.

    substitution_groups (item_id, group_id) removes pairs whose item competes
    with the treated item from the comparison group (Run 6, Phase 75): volume
    that shifts out of a repriced item and into its substitute inflates the
    measured effect from both ends.
    """
    ac = rules.actions
    window = ac.tracking_window_days
    notes: list[str] = []

    status = replay_status(events)
    implemented_on = (
        events.filter(pl.col("to_status") == "implemented")
        .sort("action_id", "seq")
        .group_by("action_id", maintain_order=True)
        .agg(pl.col("at").last().dt.date().alias("implemented_on"))
    )
    tracked = (
        actions.join(status, on="action_id", how="left", maintain_order="left")
        .filter(
            pl.col("status").is_in(["implemented", "measured"])
            & pl.col("action_type").is_in(["price_change", "below_floor_fix"])
            & pl.col("customer_id").is_not_null()
            & pl.col("item_id").is_not_null()
        )
        .join(implemented_on, on="action_id", how="inner", maintain_order="left")
    )
    if tracked.height == 0:
        return TrackingResult(
            outcomes=pl.DataFrame(schema=OUTCOMES_SCHEMA),
            calibration=_calibration(pl.DataFrame(schema=OUTCOMES_SCHEMA), rules),
            event_study=EventStudy(
                estimand=ESTIMAND, reason="no implemented price actions to measure"
            ),
            notes=["no implemented price actions to measure"],
        )

    sales = ledger.filter(
        (pl.col("transaction_type") == "sale")
        & (pl.col("quantity_norm") > 0)
        & (pl.col("net_amount_cents") > 0)
    ).select(
        "customer_id",
        "item_id",
        "invoice_date",
        "quantity_norm",
        "net_amount_cents",
        "pocket_revenue_cents",
        "pocket_gp_cents",
        "product_cost_cents",
        "contribution_cents",
    )
    max_date = sales["invoice_date"].max()

    # Pairs with a price action in flight are never controls.
    in_flight = (
        actions.join(status, on="action_id", how="left", maintain_order="left")
        .filter(
            pl.col("action_type").is_in(["price_change", "below_floor_fix"])
            & pl.col("status").is_in(["approved", "exported", "implemented", "measured"])
        )
        .select("customer_id", "item_id")
        .unique()
    )
    seg_of_pair = (
        pair_segments.filter(pl.col("segment_leaf").is_not_null()).select(
            "customer_id", "item_id", "segment_leaf"
        )
        if pair_segments is not None
        else actions.filter(pl.col("segment_leaf").is_not_null()).select(
            "customer_id", "item_id", "segment_leaf"
        )
    ).unique(subset=["customer_id", "item_id"])

    # Clean controls (Run 6, Phase 75). A comparison pair has to be untreated
    # for the WHOLE window, and untouched by anything that moves with the
    # treatment. Three things disqualify a pair, each counted and named:
    #
    #   in flight      it carries a price action at approved status or beyond,
    #                  whenever that action lands - including later in the
    #                  same window, which is the staggered-adoption trap:
    #                  comparing against a pair that is about to be treated
    #                  measures the difference between two treatments.
    #   same campaign  it shares an overlap group or scope key with the
    #                  treated action, so the same decision moved both.
    #   substitute     its item is in the same substitution group as the
    #                  treated item, so volume shifting out of the treated
    #                  pair lands in the control and doubles the apparent
    #                  effect.
    campaign_of_action: dict[str, str] = {}
    pairs_in_campaign: dict[str, set[tuple[str, str]]] = {}
    camp_col = next(
        (c for c in ("overlap_group", "scope_key") if c in actions.columns),
        None,
    )
    if camp_col is not None:
        camp = actions.filter(
            pl.col(camp_col).is_not_null()
            & pl.col("customer_id").is_not_null()
            & pl.col("item_id").is_not_null()
        ).select("action_id", camp_col, "customer_id", "item_id")
        for row in camp.iter_rows(named=True):
            key = str(row[camp_col])
            campaign_of_action[str(row["action_id"])] = key
            pairs_in_campaign.setdefault(key, set()).add(
                (str(row["customer_id"]), str(row["item_id"]))
            )
    sub_group_of_item: dict[str, str] = {}
    items_in_sub_group: dict[str, set[str]] = {}
    if substitution_groups is not None and {"item_id", "group_id"}.issubset(
        substitution_groups.columns
    ):
        for row in substitution_groups.select("item_id", "group_id").iter_rows(named=True):
            gid = str(row["group_id"])
            sub_group_of_item[str(row["item_id"])] = gid
            items_in_sub_group.setdefault(gid, set()).add(str(row["item_id"]))

    rows: list[dict[str, object]] = []
    for r in tracked.sort("action_id").iter_rows(named=True):
        impl: dt.date = r["implemented_on"]
        lo_b, hi_b = impl - dt.timedelta(days=window), impl - dt.timedelta(days=1)
        lo_a, hi_a = impl + dt.timedelta(days=1), impl + dt.timedelta(days=window)
        me = pl.DataFrame({"customer_id": [r["customer_id"]], "item_id": [r["item_id"]]})
        before = _window_stats(sales, me, pl.lit(lo_b), pl.lit(hi_b))
        after = _window_stats(sales, me, pl.lit(lo_a), pl.lit(hi_a))
        b = before.to_dicts()[0] if before.height else None
        a = after.to_dicts()[0] if after.height else None

        out: dict[str, object] = {
            "action_id": r["action_id"],
            "customer_id": r["customer_id"],
            "item_id": r["item_id"],
            "implemented_on": impl,
            "window_days": window,
            "qty_before": b["qty"] if b else None,
            "qty_after": a["qty"] if a else None,
            "price_before_cents": b["price_cents"] if b else None,
            "price_after_cents": a["price_cents"] if a else None,
            "proposed_price_cents": r["proposed_price_cents"],
            "realization_pct": None,
            "predicted_volume_ratio": r["predicted_volume_ratio"],
            "volume_treatment": r["volume_treatment"],
            "evidence_status": r["evidence_status"],
            "treated_volume_ratio": None,
            "stopped_buying": None,
            "control_volume_ratio": None,
            "n_control_pairs": 0,
            "n_control_pairs_excluded": 0,
            "control_exclusions": "",
            "control_adjusted_volume_ratio": None,
            "volume_error_log_points": None,
            "wave": f"{impl.year:04d}-{impl.month:02d}",
            "segment_leaf": r["segment_leaf"],
            "action_type": r["action_type"],
            "implementation_compliance": COMPLIANCE_UNKNOWN,
            "pocket_margin_before": b["pocket_margin"] if b else None,
            "pocket_margin_after": a["pocket_margin"] if a else None,
            "contribution_before_cents": b["contribution_cents"] if b else None,
            "contribution_after_cents": a["contribution_cents"] if a else None,
            "outcome_status": INSUFFICIENT,
            "outcome_reason": "",
        }
        if max_date is not None and max_date < hi_a:  # type: ignore[operator]
            out["outcome_reason"] = (
                f"measurement window not elapsed (data through {max_date!s}, window ends {hi_a!s})"
            )
            rows.append(out)
            continue
        # The BEFORE window is the baseline and has to be real. The AFTER
        # window is the outcome: an empty one means the pair stopped buying,
        # which is a result and the most consequential one a price increase
        # can produce. Refusing to measure it - which this gate used to do -
        # dropped exactly the cases that matter from every pooled figure.
        if b is None or int(b["n_lines"]) < ac.tracking_min_before_sales:
            have = int(b["n_lines"]) if b else 0
            out["outcome_reason"] = (
                f"no usable baseline: {have} sale line(s) in the {window} days before "
                f"implementation, against the {ac.tracking_min_before_sales} needed. "
                "Without a baseline there is nothing for the after window to be "
                "compared with."
            )
            rows.append(out)
            continue
        stopped = a is None or float(a["qty"]) <= 0
        out["stopped_buying"] = stopped
        out["qty_after"] = 0.0 if a is None else a["qty"]

        # Realization. A pair that stopped buying has no after price, so the
        # price never moved as far as the invoices are concerned - which is
        # recorded as unknown rather than as zero realization, because no
        # invoice disagreed with the new price either.
        p0, p1 = int(b["price_cents"]), r["proposed_price_cents"]
        if a is not None and a["price_cents"] is not None and p1 is not None and p1 != p0:
            out["realization_pct"] = round((int(a["price_cents"]) - p0) / (int(p1) - p0), 4)
        out["implementation_compliance"] = _compliance(out["realization_pct"])  # type: ignore[arg-type]

        # Comparison group: same segment, clean through the whole window, and
        # with a baseline of its own. A control that bought before and not
        # after contributes a ratio of zero - the same rule the treated pair
        # is held to, because restricting controls to survivors would compare
        # every treated pair against a group that could not fall to zero.
        seg = r["segment_leaf"]
        pool = (
            seg_of_pair.filter(pl.col("segment_leaf") == seg)
            .select("customer_id", "item_id")
            .unique()
        )
        n_pool = pool.height
        excluded: list[str] = []
        clean = pool.join(in_flight, on=["customer_id", "item_id"], how="anti")
        if n_pool - clean.height:
            excluded.append(f"{n_pool - clean.height} with a price action of their own")
        campaign = campaign_of_action.get(str(r["action_id"]))
        if campaign and pairs_in_campaign.get(campaign):
            mates = pl.DataFrame(
                sorted(pairs_in_campaign[campaign]),
                schema=["customer_id", "item_id"],
                orient="row",
            )
            before_n = clean.height
            clean = clean.join(mates, on=["customer_id", "item_id"], how="anti")
            if before_n - clean.height:
                excluded.append(f"{before_n - clean.height} in the same campaign")
        sub_gid = sub_group_of_item.get(str(r["item_id"]))
        if sub_gid and items_in_sub_group.get(sub_gid):
            before_n = clean.height
            clean = clean.filter(~pl.col("item_id").is_in(sorted(items_in_sub_group[sub_gid])))
            if before_n - clean.height:
                excluded.append(f"{before_n - clean.height} selling a substitute item")

        cb = _window_stats(sales, clean, pl.lit(lo_b), pl.lit(hi_b)).rename({"qty": "qty_b"})
        ca = _window_stats(sales, clean, pl.lit(lo_a), pl.lit(hi_a)).rename({"qty": "qty_a"})
        ratios = (
            cb.select("customer_id", "item_id", "qty_b")
            .join(
                ca.select("customer_id", "item_id", "qty_a"),
                on=["customer_id", "item_id"],
                how="left",
            )
            .with_columns(pl.col("qty_a").fill_null(0.0))
            .filter(pl.col("qty_b") > 0)
        )
        no_baseline = clean.height - ratios.height
        if no_baseline > 0:
            excluded.append(f"{no_baseline} with no sales before the change")
        out["n_control_pairs"] = ratios.height
        out["n_control_pairs_excluded"] = n_pool - ratios.height
        out["control_exclusions"] = "; ".join(excluded)
        treated_ratio = float(out["qty_after"]) / float(b["qty"])  # type: ignore[arg-type]
        out["treated_volume_ratio"] = round(treated_ratio, 6)
        if ratios.height < ac.tracking_min_control_pairs:
            detail = f" ({'; '.join(excluded)})" if excluded else ""
            out["outcome_reason"] = (
                f"only {ratios.height} clean comparison pair(s) in segment {seg} "
                f"out of {n_pool} in it (need {ac.tracking_min_control_pairs})"
                f"{detail}: the price effect cannot be separated from the "
                "general trend"
            )
            rows.append(out)
            continue
        control_ratio = float(ratios.select((pl.col("qty_a") / pl.col("qty_b")).median()).item())
        out["control_volume_ratio"] = round(control_ratio, 6)
        if control_ratio <= 0:
            out["outcome_reason"] = (
                "the comparison group's own volume fell to zero, so there is no "
                "general trend to divide out"
            )
            rows.append(out)
            continue
        adjusted = treated_ratio / control_ratio
        out["control_adjusted_volume_ratio"] = round(adjusted, 6)
        predicted = r["predicted_volume_ratio"]
        if predicted is not None and predicted > 0 and adjusted > 0:
            out["volume_error_log_points"] = round(math.log(adjusted) - math.log(predicted), 6)
        out["outcome_status"] = MEASURED
        stopped_note = (
            " The pair bought nothing in the after window, which is recorded as a "
            "volume ratio of zero rather than as missing data."
            if stopped
            else ""
        )
        out["outcome_reason"] = (
            f"volume change adjusted for the trend in {ratios.height} clean untreated "
            f"same-segment pairs." + stopped_note
        )
        rows.append(out)

    outcomes = pl.DataFrame(rows, schema=OUTCOMES_SCHEMA).sort("action_id")
    n_meas = int((outcomes["outcome_status"] == MEASURED).sum())
    n_stopped = int(
        outcomes.filter((pl.col("outcome_status") == MEASURED) & pl.col("stopped_buying")).height
    )
    notes.append(
        f"results tracking: {outcomes.height} implemented price action(s), "
        f"{n_meas} measured, {outcomes.height - n_meas} insufficient evidence"
        + (
            f"; {n_stopped} of the measured pairs bought nothing afterwards, counted as a "
            "volume ratio of zero rather than dropped"
            if n_stopped
            else ""
        )
    )
    notes.append(
        "a measured result is a control-adjusted volume change, not a "
        "difference-in-differences estimate: it divides out the trend in untreated "
        "pairs and carries none of the assumptions that name implies. Where a rollout "
        "can be phased, randomized, or run against a matched holdout, do that - it is "
        "the only way these numbers become an effect rather than a comparison."
    )
    # The pooled estimate uses the same clean-control rule as the per-action
    # figures: every pair in the segment pool that never carries a price
    # action of its own. Pairs treated later are excluded from the control
    # POOL here and enter the panel as treated units with their own event
    # time, which is what keeps an already-treated pair out of anyone else's
    # counterfactual.
    treatments = tracked.select("customer_id", "item_id", "implemented_on").unique()
    control_pool = seg_of_pair.join(in_flight, on=["customer_id", "item_id"], how="anti").select(
        "customer_id", "item_id"
    )
    study = run_event_study(sales, treatments, control_pool, rules)
    notes.append(
        f"pooled event study: {study.status.replace('_', ' ')} - {study.reason}"
        if study.reason
        else f"pooled event study: {study.status.replace('_', ' ')}"
    )
    return TrackingResult(
        outcomes=outcomes,
        calibration=_calibration(outcomes, rules),
        by_group=_by_group(outcomes),
        event_study=study,
        notes=notes,
    )


def _by_group(outcomes: pl.DataFrame) -> pl.DataFrame:
    """Measured results split by wave, segment, action type, evidence bucket
    and how faithfully the price actually moved.

    One pooled median hides whether a result came from one wave or five, from
    actions whose prices moved or ones that never did. Splitting it is cheap
    and is what makes the number auditable.
    """
    measured = outcomes.filter(pl.col("outcome_status") == MEASURED)
    if not measured.height:
        return pl.DataFrame(schema=BY_GROUP_SCHEMA)
    measured = measured.with_columns(
        pl.col("volume_treatment")
        .replace_strict(_BUCKET, default="unclassified_volume_treatment")
        .alias("evidence_bucket"),
        pl.col("segment_leaf").cast(pl.String).fill_null("unknown").alias("segment_label"),
    )
    frames = []
    for dimension, column in (
        ("wave", "wave"),
        ("segment", "segment_label"),
        ("action type", "action_type"),
        ("evidence", "evidence_bucket"),
        ("price actually moved", "implementation_compliance"),
    ):
        frames.append(
            measured.group_by(column)
            .agg(
                pl.len().alias("n_measured"),
                pl.col("stopped_buying").sum().cast(pl.Int64).alias("n_stopped_buying"),
                pl.col("control_adjusted_volume_ratio")
                .median()
                .alias("median_control_adjusted_volume_ratio"),
                pl.col("realization_pct").median().alias("median_realization_pct"),
            )
            .rename({column: "value"})
            .with_columns(
                pl.lit(dimension).alias("dimension"),
                pl.col("value").cast(pl.String),
            )
            .select(list(BY_GROUP_SCHEMA))
        )
    return (
        pl.concat(frames).sort(["dimension", "value"]).cast(BY_GROUP_SCHEMA)  # type: ignore[arg-type]
    )


def _calibration(outcomes: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Prediction error by evidence bucket. Below the configured minimum of
    measured actions the table says so instead of showing a number."""
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "bucket": pl.String,
        "n_measured": pl.Int64,
        "n_stopped_buying": pl.Int64,
        "median_abs_volume_error_log_points": pl.Float64,
        "median_realization_pct": pl.Float64,
        "status": pl.String,
    }
    measured = outcomes.filter(pl.col("outcome_status") == MEASURED)
    if measured.height < rules.actions.calibration_min_actions:
        return pl.DataFrame(
            {
                "bucket": ["all"],
                "n_measured": [measured.height],
                "n_stopped_buying": [int(measured["stopped_buying"].sum() or 0)],
                "median_abs_volume_error_log_points": [None],
                "median_realization_pct": [None],
                "status": ["not_enough_implemented_actions"],
            },
            schema=schema,
        )
    # Bucket by the action's volume evidence at generation time:
    # causal-supported vs IV-estimated vs fixed-volume (spec Phase 41).
    bucketed = measured.with_columns(
        pl.col("volume_treatment")
        .replace_strict(_BUCKET, default="unclassified_volume_treatment")
        .alias("bucket")
    )
    return (
        bucketed.group_by("bucket")
        .agg(
            pl.len().alias("n_measured"),
            # Pairs that stopped buying are IN this count and in the medians
            # below. Their volume error is undefined - the log of a zero ratio
            # - so they are named separately rather than dropped silently,
            # which is what an error median alone would do to them.
            pl.col("stopped_buying").sum().cast(pl.Int64).alias("n_stopped_buying"),
            pl.col("volume_error_log_points")
            .abs()
            .median()
            .alias("median_abs_volume_error_log_points"),
            pl.col("realization_pct").median().alias("median_realization_pct"),
        )
        .with_columns(pl.lit("measured").alias("status"))
        .sort("bucket")
        .cast(schema)  # type: ignore[arg-type]
    )


def measure_and_record(
    store: ActionStore,
    ledger: pl.DataFrame,
    rules: Rules,
    *,
    pair_segments: pl.DataFrame | None = None,
    at: dt.datetime | None = None,
) -> TrackingResult:
    """Run the tracker against the store, persist the outcomes table, and move
    newly measured actions implemented -> measured (who='tracker'). Idempotent:
    already-measured actions get no second event; outcomes are recomputed and
    replaced (they are derived data)."""
    actions = store.actions()
    events = store.events()
    result = track_outcomes(actions, events, ledger, rules, pair_segments)
    store.save_outcomes(result.outcomes)
    status = replay_status(events)
    newly_measured = (
        result.outcomes.filter(pl.col("outcome_status") == MEASURED)
        .join(status, on="action_id", how="left")
        .filter(pl.col("status") == "implemented")["action_id"]
        .to_list()
    )
    for action_id in newly_measured:
        store.transition(
            action_id,
            who="tracker",
            to_status="measured",
            note="outcome measured over the configured window",
            at=at,
        )
    return result
