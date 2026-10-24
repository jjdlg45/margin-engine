"""Results tracking (Phase 41): the feedback loop.

After an action is marked implemented, measure over a configurable window
(default 90 days on each side of the implementation date):

- Price realization: did the invoiced price actually move to the new price, or
  did it erode back? realization = (P_after - P_before) / (P_new - P_before),
  1.0 = fully realized, 0 = no move, negative = eroded further.
- Volume outcome vs prediction: the treated pair's volume ratio (after/before)
  against a comparison group of similar untreated pairs (same segment, no
  price action in flight) — a difference-in-differences comparison, so general
  trends never masquerade as price effects. The engine's prediction is the
  predicted_volume_ratio stored on the action at generation time (1.0 for
  fixed-volume actions).
- Margin outcome: pocket margin and contribution, before vs after.

Every gate fails closed: too few after-window sales, a window that has not
elapsed yet, or too few control pairs produce `insufficient_evidence` with the
reason — never a number that cannot be supported. Calibration (prediction
error by evidence bucket) appears only once enough actions are measured;
below the configured minimum it says "not enough implemented actions yet".
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import polars as pl

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
    "control_volume_ratio": pl.Float64,
    "n_control_pairs": pl.Int64,
    "did_volume_ratio": pl.Float64,
    "volume_error_log_points": pl.Float64,
    "pocket_margin_before": pl.Float64,
    "pocket_margin_after": pl.Float64,
    "contribution_before_cents": pl.Int64,
    "contribution_after_cents": pl.Int64,
    "outcome_status": pl.String,
    "outcome_reason": pl.String,
}

# Evidence buckets for the calibration report (prediction error by evidence).
_BUCKET = {
    "expected_change": "causal_supported",
    "sensitivity_range_only": "iv_estimated",
}


@dataclass
class TrackingResult:
    outcomes: pl.DataFrame
    calibration: pl.DataFrame
    notes: list[str] = field(default_factory=list)


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
) -> TrackingResult:
    """pair_segments (customer_id, item_id, segment_leaf) widens the
    comparison-group pool to every segmented pair (normally the guidance
    table); without it, only pairs present in the actions table can serve as
    controls — stated in the per-action reason when that pool is too thin."""
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
            "control_volume_ratio": None,
            "n_control_pairs": 0,
            "did_volume_ratio": None,
            "volume_error_log_points": None,
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
        if b is None or a is None or int(a["n_lines"]) < ac.tracking_min_after_sales:
            out["outcome_reason"] = (
                "not enough sales around the implementation date "
                f"(need {ac.tracking_min_after_sales} sale lines after; the pair "
                "may have stopped buying — itself a finding, reported, not inferred)"
            )
            rows.append(out)
            continue

        # Realization.
        p0, p1 = int(b["price_cents"]), r["proposed_price_cents"]
        pa = int(a["price_cents"])
        if p1 is not None and p1 != p0:
            out["realization_pct"] = round((pa - p0) / (int(p1) - p0), 4)

        # Comparison group: same segment, untreated, bought in BOTH windows.
        seg = r["segment_leaf"]
        controls = (
            seg_of_pair.filter(pl.col("segment_leaf") == seg)
            .join(in_flight, on=["customer_id", "item_id"], how="anti")
            .select("customer_id", "item_id")
            .unique()
        )
        cb = _window_stats(sales, controls, pl.lit(lo_b), pl.lit(hi_b)).rename({"qty": "qty_b"})
        ca = _window_stats(sales, controls, pl.lit(lo_a), pl.lit(hi_a)).rename({"qty": "qty_a"})
        both = cb.select("customer_id", "item_id", "qty_b").join(
            ca.select("customer_id", "item_id", "qty_a"), on=["customer_id", "item_id"], how="inner"
        )
        both = both.filter((pl.col("qty_b") > 0) & (pl.col("qty_a") > 0))
        out["n_control_pairs"] = both.height
        treated_ratio = float(a["qty"]) / float(b["qty"]) if float(b["qty"]) > 0 else None
        out["treated_volume_ratio"] = round(treated_ratio, 6) if treated_ratio is not None else None
        if both.height < ac.tracking_min_control_pairs:
            out["outcome_reason"] = (
                f"only {both.height} untreated comparison pair(s) in segment "
                f"{seg} with sales in both windows "
                f"(need {ac.tracking_min_control_pairs}): the price effect "
                "cannot be separated from the general trend"
            )
            rows.append(out)
            continue
        control_ratio = float(both.select((pl.col("qty_a") / pl.col("qty_b")).median()).item())
        out["control_volume_ratio"] = round(control_ratio, 6)
        if treated_ratio is None or control_ratio <= 0:
            out["outcome_reason"] = "degenerate volume ratios"
            rows.append(out)
            continue
        did = treated_ratio / control_ratio
        out["did_volume_ratio"] = round(did, 6)
        predicted = r["predicted_volume_ratio"]
        if predicted is not None and predicted > 0 and did > 0:
            out["volume_error_log_points"] = round(math.log(did) - math.log(predicted), 6)
        out["outcome_status"] = MEASURED
        out["outcome_reason"] = (
            f"measured against {both.height} untreated same-segment pairs "
            "(difference-in-differences)"
        )
        rows.append(out)

    outcomes = pl.DataFrame(rows, schema=OUTCOMES_SCHEMA).sort("action_id")
    n_meas = int((outcomes["outcome_status"] == MEASURED).sum())
    notes.append(
        f"results tracking: {outcomes.height} implemented price action(s), "
        f"{n_meas} measured, {outcomes.height - n_meas} insufficient evidence"
    )
    return TrackingResult(outcomes=outcomes, calibration=_calibration(outcomes, rules), notes=notes)


def _calibration(outcomes: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Prediction error by evidence bucket. Below the configured minimum of
    measured actions the table says so instead of showing a number."""
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "bucket": pl.String,
        "n_measured": pl.Int64,
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
                "median_abs_volume_error_log_points": [None],
                "median_realization_pct": [None],
                "status": ["not_enough_implemented_actions"],
            },
            schema=schema,
        )
    # Bucket by the action's volume evidence at generation time:
    # causal-supported vs IV-estimated vs fixed-volume (spec Phase 41).
    bucketed = measured.with_columns(
        pl.col("volume_treatment").replace_strict(_BUCKET, default="fixed_volume").alias("bucket")
    )
    return (
        bucketed.group_by("bucket")
        .agg(
            pl.len().alias("n_measured"),
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
