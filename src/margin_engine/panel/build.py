"""Customer x item x month panel for sensitivity estimation (Phase 6).

Zero-quantity rows exist ONLY where the pair is in active assortment (bought in
the prior N months, config) and the item was available that month (proxy: the
item sold to at least one customer that month — the engine cannot observe
stockouts directly, so availability is inferred from market-wide activity and
the proxy is labeled).

Prices for zero rows are the pair's last observed unit price (carried forward),
flagged `price_carried_forward` so no one mistakes them for transactions.

Instruments come from reference tables (supplier cost index, tariff schedule),
joined by the item's supplier / category and month, exactly as a real deployment
would obtain them.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from margin_engine.config import Rules


def month_index(col: str = "invoice_date") -> pl.Expr:
    return (pl.col(col).dt.year() * 12 + pl.col(col).dt.month()).alias("month_idx")


def build_panel(
    observations: pl.DataFrame,
    items: pl.DataFrame | None,
    supplier_cost_index: pl.DataFrame | None,
    tariff_schedule: pl.DataFrame | None,
    rules: Rules,
    market_price_index: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Returns one row per (customer, item, month) with qty (including true
    zeros), log price, instruments, and cluster columns."""
    lookback = rules.sensitivity.active_assortment_lookback_months

    sales = observations.filter(
        (pl.col("quantity_norm") > 0) & (pl.col("net_amount_cents") > 0)
    ).with_columns(month_index())
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

    # Item availability proxy: item sold to anyone that month.
    item_avail = (
        monthly.select("item_id", "month_idx")
        .unique()
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
        pl.col("item_available").fill_null(False)
    )
    # Keep purchases plus true zeros (active AND available).
    panel = panel.filter((pl.col("qty") > 0) | (pl.col("active") & pl.col("item_available")))

    # Carry forward the last observed price for zero rows (labeled).
    panel = panel.with_columns(
        pl.col("unit_price_cents_f")
        .forward_fill()
        .over("customer_id", "item_id")
        .alias("price_cf"),
        (pl.col("qty") == 0).alias("price_carried_forward"),
    ).filter(pl.col("price_cf").is_not_null())

    # Attributes and instruments.
    if items is not None:
        # A minimal real-world items table may carry only id + description:
        # join whatever attribute columns actually exist.
        cols = ["item_id"] + [
            c for c in ("supplier_id", "item_category", "item_role") if c in items.columns
        ]
        if len(cols) > 1:
            panel = panel.join(items.select(cols), on="item_id", how="left")
    year = (pl.col("month_idx") - 1) // 12
    mon = (pl.col("month_idx") - 1) % 12 + 1
    panel = panel.with_columns(
        (year.cast(pl.String) + "-" + mon.cast(pl.String).str.zfill(2)).alias("period")
    )
    if supplier_cost_index is not None:
        sci = supplier_cost_index.with_columns(
            pl.col("cost_idx").cast(pl.Float64).log().alias("z_cost")
        )
        panel = panel.join(
            sci.select("supplier_id", "period", "z_cost"),
            on=["supplier_id", "period"],
            how="left",
        )
    if tariff_schedule is not None:
        ts = tariff_schedule.with_columns(
            (1 + pl.col("duty_pct").cast(pl.Float64) / 100).log().alias("z_tariff")
        )
        panel = panel.join(
            ts.select("item_category", "period", "z_tariff"),
            on=["item_category", "period"],
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
            mpi.select("item_category", "period", "log_mkt"),
            on=["item_category", "period"],
            how="left",
        )

    panel = panel.with_columns(
        (pl.col("customer_id") + "|" + pl.col("item_id")).alias("pair"),
        (pl.col("price_cf") / 100).log().alias("log_price"),
        pl.when(pl.col("qty") > 0).then(pl.col("qty").log()).otherwise(None).alias("log_qty"),
        (pl.col("supplier_id") + "@" + pl.col("period")).alias("cluster_supplier_month")
        if "supplier_id" in (items.columns if items is not None else [])
        or supplier_cost_index is not None
        else pl.lit("all").alias("cluster_supplier_month"),
        pl.col("period").alias("cluster_month"),
        pl.col("customer_id").alias("cluster_customer"),
        (
            (pl.col("item_role").fill_null("unknown") + "@" + pl.col("period"))
            if items is not None and "item_role" in items.columns
            else pl.lit("all@") + pl.col("period")
        ).alias("role_month"),
        pl.col("segment_leaf_pair").alias("segment_leaf"),
        pl.col("contract_id_pair").alias("contract_id"),
    )
    return panel.drop("_buy_m", "_last_buy_before", "first_m", "last_m")
