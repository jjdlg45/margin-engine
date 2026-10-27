"""Customer x item x month panel for sensitivity estimation (Phase 6).

Zero-quantity rows exist ONLY where the pair is in active assortment (bought in
the prior N months, config) and the item was available that month (proxy: the
item sold to at least one customer that month — the engine cannot observe
stockouts directly, so availability is inferred from market-wide activity and
the proxy is labeled).

Zero rows still need a price, and which price is used decides what the
extensive margin can identify (Run 6, Phase 72). A carried-forward price has no
within-pair variation during a zero run, which attenuates the estimate and
dilutes the first stage, so it is the LAST resort rather than the only option.
The ladder, best source first:

1. `quoted` - the customer actually asked for a price that month and did not
   buy. A lost quote is the one genuinely observed price for a non-purchase.
2. `carried_forward` - the pair's last observed unit price.

A third rung, `list_x_pair_discount` (the effective-dated list for that month
times the pair's own trailing discount), is implemented and selectable but is
NOT in the default ladder, because measurement said it does harm:

  - it barely adds within-pair price variation, the thing it was meant to add -
    median within-pair log-price standard deviation moved 0.0655 to 0.0662,
    because a carried-forward price already tracks the list closely;
  - it makes the total-quantity estimate WORSE once the first stage is fitted on
    observed prices only (median absolute error against planted truth 0.063 with
    carry-forward against 0.108 with the list rung): the extra movement is list
    movement the customer never faced, so in the second stage it is noise;
  - and it widens the imputed-price robustness gap the causal gate measures,
    because the estimate becomes more sensitive to how the zero rows were
    priced - which is the gate doing its job.

It stays available for the robustness variant and for deployments whose list is
a better guide to an unobserved price than the last negotiated one.

Every row records `price_status` naming its source, and purchases carry
`observed`. `price_carried_forward` remains as the flag for the last rung.

Instruments come from reference tables (supplier cost index, tariff schedule),
joined by the item's supplier / category and month, exactly as a real deployment
would obtain them.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from margin_engine.config import Rules
from margin_engine.fiscal import FiscalCalendar, build_calendar


def month_index(col: str = "invoice_date") -> pl.Expr:
    return (pl.col(col).dt.year() * 12 + pl.col(col).dt.month()).alias("month_idx")


# The instruments are ASSIGNED at supplier x month and category x month, so
# those are the levels at which their variation is correlated (Run 6, Phase
# 73, Finding 4). When the shocks are serially correlated within a supplier -
# the default generator draws them as AR(1), and real input costs behave the
# same way - supplier x month is too FINE: the correlation runs across months
# within the supplier, which supplier x month treats as independent. Clustering
# at the assignment unit itself (supplier, category) is the coarser, safer
# choice, at the cost of far fewer clusters.
#
# Every option is built here so the estimator can switch between them without
# rebuilding the panel, and so the per-cluster instrument-variation report can
# be produced for any of them. A level whose identifying column is absent
# collapses to one cluster, which the small-cluster guard catches and reports
# rather than silently treating as valid inference.
_CLUSTER_SOURCES: dict[str, tuple[str, ...]] = {
    "cluster_supplier": ("supplier_id",),
    "cluster_supplier_month": ("supplier_id", "period"),
    "cluster_category": ("item_category",),
    "cluster_category_month": ("item_category", "period"),
    "cluster_month": ("period",),
    "cluster_customer": ("customer_id",),
}


def _cluster_columns(panel: pl.DataFrame) -> list[pl.Expr]:
    """Build every clustering key the estimator can be configured to use."""
    out: list[pl.Expr] = []
    for name, sources in _CLUSTER_SOURCES.items():
        if not all(c in panel.columns for c in sources):
            # No column to cluster on. One group is not a usable cluster
            # structure; it is reported as such by the cluster-adequacy gate.
            out.append(pl.lit("unavailable").alias(name))
            continue
        expr = pl.col(sources[0]).fill_null("unknown")
        for c in sources[1:]:
            expr = expr + "@" + pl.col(c).fill_null("unknown")
        out.append(expr.alias(name))
    return out


def build_panel(
    observations: pl.DataFrame,
    items: pl.DataFrame | None,
    supplier_cost_index: pl.DataFrame | None,
    tariff_schedule: pl.DataFrame | None,
    rules: Rules,
    market_price_index: pl.DataFrame | None = None,
    fiscal: FiscalCalendar | None = None,
    quotes: pl.DataFrame | None = None,
    price_list: pl.DataFrame | None = None,
    zero_price_sources: tuple[str, ...] = ("quoted", "carried_forward"),
) -> pl.DataFrame:
    """Returns one row per (customer, item, period) with qty (including true
    zeros), log price, instruments, and cluster columns. The period follows the
    configured fiscal calendar (Phase 36); month_idx is the consecutive fiscal
    period index and reproduces the historical year*12+month under the default
    calendar. Monthly instrument and market-index tables join by their own
    data months under the default calendar; under a week-based or custom
    calendar they join by the calendar month containing each fiscal period's
    start (a documented approximation, noted by the caller)."""
    if fiscal is None:
        fiscal = build_calendar(rules.fiscal)
    lookback = rules.sensitivity.active_assortment_lookback_months

    sales = observations.filter((pl.col("quantity_norm") > 0) & (pl.col("net_amount_cents") > 0))
    if fiscal.is_calendar_months:
        sales = sales.with_columns(month_index())
    else:
        sales = fiscal.attach(sales, "invoice_date", idx="month_idx")
    period_map: pl.DataFrame | None = None
    if not fiscal.is_calendar_months and sales.height:
        d = sales["invoice_date"]
        period_map = fiscal.index_table(d.min(), d.max()).select(  # type: ignore[arg-type]
            pl.col("period_idx").alias("month_idx"),
            pl.col("period"),
            pl.col("start").dt.strftime("%Y-%m").alias("_cal_period"),
        )
    if "flag_data_quality_anomaly" in sales.columns:
        # Entry errors are not prices; a x10 line would be a massive leverage
        # point in the within-pair regression.
        sales = sales.filter(~pl.col("flag_data_quality_anomaly"))
    if "flag_commercial_pricing_outlier" in sales.columns:
        # One-off anomalous prices (clearance, strategic concessions) are real
        # dollars but not demand-curve observations; standard robust practice
        # excludes them from behavioral estimation. They remain in the ledger.
        # On SPARSE pairs the one-off judgment is unreliable (a genuine
        # shock-driven price move looks like a one-off against 3-4 points of
        # history) and excluding those points provably biases sparse-segment
        # elasticities away from zero, so the exclusion requires enough pair
        # history to mean something (threshold in config, 24.14).
        min_hist = rules.sensitivity.outlier_exclusion_min_pair_lines
        reliable = pl.col("_pair_n") >= min_hist if "_pair_n" in sales.columns else pl.lit(True)
        sales = sales.filter(~(pl.col("flag_commercial_pricing_outlier") & reliable))

    # Stable row order before aggregation: the float qty sum and the first()
    # picks below must not depend on upstream join order.
    sales = sales.sort("source_line_key")

    monthly = (
        sales.group_by("customer_id", "item_id", "month_idx")
        .agg(
            pl.col("quantity_norm").sum().alias("qty"),
            pl.col("net_amount_cents").sum().alias("net_cents"),
            pl.col("segment_leaf").drop_nulls().first().alias("segment_leaf")
            if "segment_leaf" in sales.columns
            else pl.lit(None, dtype=pl.Int64).alias("segment_leaf"),
            pl.col("contract_id").drop_nulls().first().alias("contract_id"),
        )
        .with_columns((pl.col("net_cents") / pl.col("qty")).alias("unit_price_cents_f"))
    )

    months = monthly["month_idx"].unique().sort()
    m_min = int(months.min() or 0)  # type: ignore[arg-type]
    m_max = int(months.max() or 0)  # type: ignore[arg-type]
    all_months = pl.DataFrame({"month_idx": np.arange(m_min, m_max + 1, dtype=np.int64)})

    # Item availability proxy: item sold to anyone that month. The count of
    # distinct buyers behind that proxy is kept (Run 6, Phase 72), because one
    # buyer is a much weaker claim that the item was available to everyone else
    # than twenty are, and the robustness check re-estimates without the rows
    # that rest on the weakest evidence.
    item_avail = (
        monthly.group_by("item_id", "month_idx")
        .agg(pl.col("customer_id").n_unique().alias("availability_buyers"))
        .with_columns(pl.lit(True).alias("item_available"))
    )

    # Pair-month scaffold: for each pair, months from first purchase to horizon end.
    pair_first = monthly.group_by("customer_id", "item_id").agg(
        pl.col("month_idx").min().alias("first_m"),
        pl.col("month_idx").max().alias("last_m"),
        pl.col("segment_leaf").drop_nulls().first().alias("segment_leaf_pair"),
        pl.col("contract_id").drop_nulls().first().alias("contract_id_pair"),
    )
    scaffold = pair_first.join(all_months, how="cross").filter(
        pl.col("month_idx") >= pl.col("first_m")
    )
    panel = scaffold.join(
        monthly.select(
            "customer_id", "item_id", "month_idx", "qty", "net_cents", "unit_price_cents_f"
        ),
        on=["customer_id", "item_id", "month_idx"],
        how="left",
    ).with_columns(pl.col("qty").fill_null(0.0))

    # Active assortment: purchased within the prior `lookback` months.
    panel = panel.sort("customer_id", "item_id", "month_idx").with_columns(
        pl.when(pl.col("qty") > 0).then(pl.col("month_idx")).otherwise(None).alias("_buy_m")
    )
    panel = panel.with_columns(
        pl.col("_buy_m")
        .shift(1)
        .forward_fill()
        .over("customer_id", "item_id")
        .alias("_last_buy_before")
    )
    active = (pl.col("qty") > 0) | ((pl.col("month_idx") - pl.col("_last_buy_before")) <= lookback)
    panel = panel.with_columns(active.fill_null(False).alias("active"))

    panel = panel.join(item_avail, on=["item_id", "month_idx"], how="left").with_columns(
        pl.col("item_available").fill_null(False),
        pl.col("availability_buyers").fill_null(0),
    )
    # Keep purchases plus true zeros (active AND available).
    panel = panel.filter((pl.col("qty") > 0) | (pl.col("active") & pl.col("item_available")))

    # Price the zero rows from the best available source (Run 6, Phase 72).
    # Each rung is computed for every zero row that can have it; the ladder then
    # takes the first one present, in the configured order, and records which.
    panel = panel.with_columns(
        pl.col("unit_price_cents_f")
        .forward_fill()
        .over("customer_id", "item_id")
        .alias("_price_carried_forward_cents"),
    )

    rung_cols: dict[str, str] = {}
    if "quoted" in zero_price_sources and quotes is not None and quotes.height:
        # A quote the customer did not convert is a real, observed price for a
        # non-purchase - the only rung that is not an imputation.
        q = quotes
        if "quote_date" in q.columns:
            q = q.with_columns(month_index("quote_date"))
        need = {"customer_id", "item_id", "month_idx", "quoted_price_cents"}
        if need <= set(q.columns):
            lost = q
            if "outcome" in q.columns:
                lost = q.filter(pl.col("outcome") != "W")
            lost = (
                lost.group_by("customer_id", "item_id", "month_idx")
                .agg(pl.col("quoted_price_cents").median().alias("_price_quoted_cents"))
                .with_columns(pl.col("_price_quoted_cents").cast(pl.Float64))
            )
            panel = panel.join(lost, on=["customer_id", "item_id", "month_idx"], how="left")
            rung_cols["quoted"] = "_price_quoted_cents"

    if "list_x_pair_discount" in zero_price_sources and price_list is not None:
        need = {"item_id", "effective_date", "list_price_scaled"}
        if need <= set(price_list.columns):
            # Effective-dated list for the month, joined as-of the month start,
            # times the pair's own trailing discount to list. The list moves,
            # so this rung carries genuine within-pair variation.
            lp = (
                price_list.with_columns(month_index("effective_date"))
                .sort("item_id", "month_idx")
                .group_by("item_id", "month_idx")
                .agg(pl.col("list_price_scaled").last().alias("_list_scaled"))
                .sort("item_id", "month_idx")
            )
            panel = panel.sort("item_id", "month_idx").join_asof(
                lp,
                on="month_idx",
                by="item_id",
                strategy="backward",
                check_sortedness=False,
            )
            panel = panel.sort("customer_id", "item_id", "month_idx")
            panel = panel.with_columns((pl.col("_list_scaled") / 100.0).alias("_list_cents_f"))
            # The pair's discount to list, from its purchases only, carried
            # forward so a zero row uses the discount the pair last negotiated.
            panel = panel.with_columns(
                pl.when((pl.col("qty") > 0) & (pl.col("_list_cents_f") > 0))
                .then(pl.col("unit_price_cents_f") / pl.col("_list_cents_f"))
                .otherwise(None)
                .alias("_pair_index_obs")
            )
            panel = panel.with_columns(
                pl.col("_pair_index_obs")
                .forward_fill()
                .over("customer_id", "item_id")
                .alias("_pair_index")
            )
            panel = panel.with_columns(
                pl.when((pl.col("_list_cents_f") > 0) & pl.col("_pair_index").is_not_null())
                .then(pl.col("_list_cents_f") * pl.col("_pair_index"))
                .otherwise(None)
                .alias("_price_list_x_discount_cents")
            )
            rung_cols["list_x_pair_discount"] = "_price_list_x_discount_cents"

    if "carried_forward" in zero_price_sources:
        rung_cols["carried_forward"] = "_price_carried_forward_cents"

    price_expr: pl.Expr = pl.lit(None, dtype=pl.Float64)
    status_expr: pl.Expr = pl.lit(None, dtype=pl.String)
    for name in reversed(zero_price_sources):
        col = rung_cols.get(name)
        if col is None or col not in panel.columns:
            continue
        usable = pl.col(col).is_not_null() & (pl.col(col) > 0)
        price_expr = pl.when(usable).then(pl.col(col)).otherwise(price_expr)
        status_expr = pl.when(usable).then(pl.lit(name)).otherwise(status_expr)
    panel = panel.with_columns(
        pl.when(pl.col("qty") > 0)
        .then(pl.col("unit_price_cents_f"))
        .otherwise(price_expr)
        .alias("price_cf"),
        pl.when(pl.col("qty") > 0)
        .then(pl.lit("observed"))
        .otherwise(status_expr)
        .alias("price_status"),
    )
    panel = panel.with_columns(
        (pl.col("price_status") == "carried_forward").alias("price_carried_forward")
    ).filter(pl.col("price_cf").is_not_null())

    # What is actually known about each zero row (Run 6, Phase 72). Three
    # separate claims, each labeled for what it rests on rather than folded
    # into one "this is a true zero" flag:
    #
    #   assortment_basis   - `observed` when a purchase was made, `inferred`
    #                        when the pair is only assumed to still be carrying
    #                        the item because it bought within the lookback.
    #   availability_basis - `observed` for a purchase; otherwise `proxied`,
    #                        because the engine cannot see stock. The number of
    #                        distinct buyers behind the proxy rides alongside.
    #   zero_reason_basis  - `not_applicable` for a purchase, otherwise
    #                        `unknown`: no ERP export says WHY a customer did
    #                        not buy, and inventing a reason would be the kind
    #                        of guess this engine refuses elsewhere.
    panel = panel.with_columns(
        pl.when(pl.col("qty") > 0)
        .then(pl.lit("observed"))
        .otherwise(pl.lit("inferred"))
        .alias("assortment_basis"),
        pl.when(pl.col("qty") > 0)
        .then(pl.lit("observed"))
        .otherwise(pl.lit("proxied"))
        .alias("availability_basis"),
        pl.when(pl.col("qty") > 0)
        .then(pl.lit("not_applicable"))
        .otherwise(pl.lit("unknown"))
        .alias("zero_reason_basis"),
    )
    panel = panel.drop(
        [
            c
            for c in panel.columns
            if c.startswith("_price_")
            or c in ("_list_scaled", "_list_cents_f", "_pair_index_obs", "_pair_index")
        ]
    )

    # Attributes and instruments.
    if items is not None:
        # A minimal real-world items table may carry only id + description:
        # join whatever attribute columns actually exist.
        cols = ["item_id"] + [
            c for c in ("supplier_id", "item_category", "item_role") if c in items.columns
        ]
        if len(cols) > 1:
            panel = panel.join(items.select(cols), on="item_id", how="left")
    if period_map is None:
        year = (pl.col("month_idx") - 1) // 12
        mon = (pl.col("month_idx") - 1) % 12 + 1
        panel = panel.with_columns(
            (year.cast(pl.String) + "-" + mon.cast(pl.String).str.zfill(2)).alias("period")
        )
        # Monthly instrument tables carry data months; under the default
        # calendar the panel period IS that month.
        join_key = "period"
    else:
        panel = panel.join(period_map, on="month_idx", how="left", maintain_order="left")
        join_key = "_cal_period"
    if supplier_cost_index is not None:
        sci = supplier_cost_index.with_columns(
            pl.col("cost_idx").cast(pl.Float64).log().alias("z_cost")
        )
        panel = panel.join(
            sci.select("supplier_id", pl.col("period").alias(join_key), "z_cost"),
            on=["supplier_id", join_key],
            how="left",
        )
    if tariff_schedule is not None:
        ts = tariff_schedule.with_columns(
            (1 + pl.col("duty_pct").cast(pl.Float64) / 100).log().alias("z_tariff")
        )
        panel = panel.join(
            ts.select("item_category", pl.col("period").alias(join_key), "z_tariff"),
            on=["item_category", join_key],
            how="left",
        )
    # Optional market / competitor price index (24.10): a log control in the
    # elasticity models. Industry-wide cost shocks also move competitor prices,
    # so without this control estimates likely understate own-price
    # sensitivity (documented limitation; overidentification cannot detect it).
    if market_price_index is not None and "item_category" in panel.columns:
        mpi = market_price_index.with_columns(
            pl.col("market_index").cast(pl.Float64).log().alias("log_mkt")
        )
        panel = panel.join(
            mpi.select("item_category", pl.col("period").alias(join_key), "log_mkt"),
            on=["item_category", join_key],
            how="left",
        )

    panel = panel.with_columns(
        (pl.col("customer_id") + "|" + pl.col("item_id")).alias("pair"),
        (pl.col("price_cf") / 100).log().alias("log_price"),
        pl.when(pl.col("qty") > 0).then(pl.col("qty").log()).otherwise(None).alias("log_qty"),
        *_cluster_columns(panel),
        (
            (pl.col("item_role").fill_null("unknown") + "@" + pl.col("period"))
            if items is not None and "item_role" in items.columns
            else pl.lit("all@") + pl.col("period")
        ).alias("role_month"),
        pl.col("segment_leaf_pair").alias("segment_leaf"),
        pl.col("contract_id_pair").alias("contract_id"),
    )
    return panel.drop(
        "_buy_m", "_last_buy_before", "first_m", "last_m", "_cal_period", strict=False
    )
