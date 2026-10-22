"""Descriptive analytics (Phase 3): normalized price index, peer-group
dispersion, outlier flags, ABC classifications.

Outliers use the modified robust z-score 0.6745 * (x - median) / MAD at the
configured threshold within peer groups; the MAD = 0 fallback flags prices more
than the configured % away from the group's modal price. The method actually
used is labeled per line. Two flags are kept strictly separate:
data_quality_anomaly (likely entry errors) and commercial_pricing_outlier
(genuine but extreme pricing). Original records are never modified.
"""

from __future__ import annotations

import polars as pl

from margin_engine.config import Rules

PRICE_INDEX_VS_LIST = "price_vs_effective_list"
PRICE_INDEX_VS_ITEM_MEDIAN = "price_vs_item_median"
# A flagged line at least this many times (or 1/x) the peer median is treated
# as a data-entry anomaly rather than commercial pricing.
DATA_QUALITY_RATIO = 3.0


def normalized_price(observations: pl.DataFrame) -> pl.DataFrame:
    """Add unit_price_norm_cents, price_index and price_index_method.

    Index = realized unit price / effective-dated list where list exists,
    else unit price / item median unit price (labeled). Never both mixed
    silently.
    """
    df = observations.with_columns(
        (pl.col("net_amount_cents") / pl.col("quantity_norm")).alias("_unit_price_norm")
    )
    df = df.with_columns(
        pl.col("_unit_price_norm").median().over("item_id").alias("_item_median_price")
    )
    return df.with_columns(
        pl.when(pl.col("eff_list_cents").is_not_null() & (pl.col("eff_list_cents") > 0))
        .then(pl.col("_unit_price_norm") / pl.col("eff_list_cents"))
        .when(pl.col("_item_median_price") > 0)
        .then(pl.col("_unit_price_norm") / pl.col("_item_median_price"))
        .otherwise(None)
        .alias("price_index"),
        pl.when(pl.col("eff_list_cents").is_not_null() & (pl.col("eff_list_cents") > 0))
        .then(pl.lit(PRICE_INDEX_VS_LIST))
        .when(pl.col("_item_median_price") > 0)
        .then(pl.lit(PRICE_INDEX_VS_ITEM_MEDIAN))
        .otherwise(pl.lit("unavailable"))
        .alias("price_index_method"),
        pl.col("_unit_price_norm").alias("unit_price_norm_cents"),
    ).drop("_item_median_price")


def assign_peer_groups(df: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Peer group: item x UOM x contract regime x quarter, falling back to the
    item family when the cohort is smaller than the configured minimum."""
    df = df.with_columns(
        pl.when(pl.col("contract_id").is_not_null())
        .then(pl.lit("contract"))
        .otherwise(pl.lit("open"))
        .alias("contract_regime"),
        (
            pl.col("invoice_date").dt.year().cast(pl.String)
            + "Q"
            + pl.col("invoice_date").dt.quarter().cast(pl.String)
        ).alias("quarter"),
    )
    item_group = pl.concat_str(
        [pl.col("item_id"), pl.col("uom_norm"), pl.col("contract_regime"), pl.col("quarter")],
        separator="|",
    )
    fam_group = pl.concat_str(
        [
            pl.col("item_family").fill_null("UNKNOWN_FAMILY"),
            pl.col("uom_norm"),
            pl.col("contract_regime"),
            pl.col("quarter"),
        ],
        separator="|",
    )
    df = df.with_columns(item_group.alias("_pg_item"), fam_group.alias("_pg_family"))
    df = df.with_columns(pl.len().over("_pg_item").alias("_pg_item_n"))
    min_n = rules.outliers.min_peer_group_size
    return df.with_columns(
        pl.when(pl.col("_pg_item_n") >= min_n)
        .then(pl.col("_pg_item"))
        .otherwise(pl.col("_pg_family"))
        .alias("peer_group"),
        pl.when(pl.col("_pg_item_n") >= min_n)
        .then(pl.lit("item"))
        .otherwise(pl.lit("item_family"))
        .alias("peer_group_level"),
    ).drop("_pg_item", "_pg_family", "_pg_item_n")


def flag_outliers(df: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Modified robust z within peer group; MAD=0 fallback on modal price.

    Adds: robust_z, outlier_method, flag_data_quality_anomaly,
    flag_commercial_pricing_outlier. The two flags are mutually exclusive.
    """
    oc = rules.outliers
    k = oc.mad_consistency_constant
    df = (
        df.with_columns(pl.col("price_index").median().over("peer_group").alias("_pg_median"))
        .with_columns((pl.col("price_index") - pl.col("_pg_median")).abs().alias("_absdev"))
        .with_columns(
            pl.col("_absdev").median().over("peer_group").alias("_pg_mad"),
            pl.len().over("peer_group").alias("_pg_n"),
        )
    )
    # Modal price for the MAD=0 fallback: most frequent rounded index.
    df = df.with_columns(pl.col("price_index").round(4).alias("_pi_r"))
    mode = (
        df.group_by("peer_group", "_pi_r")
        .len()
        .sort(["peer_group", "len", "_pi_r"], descending=[False, True, False])
        .group_by("peer_group", maintain_order=True)
        .first()
        .select("peer_group", pl.col("_pi_r").alias("_pg_mode"))
    )
    df = df.join(mode, on="peer_group", how="left")

    mad_ok = pl.col("_pg_mad") > 0
    z = k * (pl.col("price_index") - pl.col("_pg_median")) / pl.col("_pg_mad")
    fallback_dev = (pl.col("price_index") / pl.col("_pg_mode") - 1).abs()
    too_small = pl.col("_pg_n") < 5  # degenerate cohorts: no flagging, labeled

    df = df.with_columns(
        pl.when(mad_ok).then(z).otherwise(None).alias("robust_z"),
        pl.when(too_small)
        .then(pl.lit("cohort_too_small"))
        .when(mad_ok)
        .then(pl.lit("modified_robust_zscore"))
        .otherwise(pl.lit("mad_zero_modal_fallback"))
        .alias("outlier_method"),
    )
    peer_outlier = (
        pl.when(too_small)
        .then(False)
        .when(mad_ok)
        .then(pl.col("robust_z").abs() > oc.zscore_threshold)
        .otherwise(fallback_dev > oc.mad_zero_fallback_pct)
    )

    # Second stage: one-off deviations from the line's own customer x item
    # typical price. Peer groups mix customer segments, so a deep one-off
    # discount can hide inside wide peer dispersion while being glaring
    # against the pair's own history. Same robust z, same threshold, within
    # the same peer group; the firing method is labeled.
    if "customer_id" not in df.columns or "item_id" not in df.columns:
        ratio0 = pl.col("price_index") / pl.col("_pg_median")
        entry_err0 = (ratio0 >= DATA_QUALITY_RATIO) | (ratio0 <= 1 / DATA_QUALITY_RATIO)
        return df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("pair_residual_z"),
            (peer_outlier & entry_err0).alias("flag_data_quality_anomaly"),
            (peer_outlier & ~entry_err0).alias("flag_commercial_pricing_outlier"),
        ).drop("_absdev", "_pi_r")
    df = df.with_columns(
        pl.col("price_index").median().over("customer_id", "item_id").alias("_pair_med"),
        pl.len().over("customer_id", "item_id").alias("_pair_n"),
    ).with_columns((pl.col("price_index") / pl.col("_pair_med")).alias("_resid"))
    df = df.with_columns(
        pl.col("_resid").median().over("peer_group").alias("_r_med"),
    ).with_columns(
        (pl.col("_resid") - pl.col("_r_med")).abs().median().over("peer_group").alias("_r_mad")
    )
    resid_z = k * (pl.col("_resid") - pl.col("_r_med")) / pl.col("_r_mad")
    resid_outlier = (
        (pl.col("_pair_n") >= 3)
        & (pl.col("_r_mad") > 0)
        & ~too_small
        & (resid_z.abs() > oc.zscore_threshold)
    )
    df = df.with_columns(
        pl.when(pl.col("_r_mad") > 0).then(resid_z).otherwise(None).alias("pair_residual_z"),
        pl.when(peer_outlier)
        .then(pl.col("outlier_method"))
        .when(resid_outlier)
        .then(pl.lit("pair_residual_zscore"))
        .otherwise(pl.col("outlier_method"))
        .alias("outlier_method"),
    )
    is_outlier = peer_outlier | resid_outlier
    ratio = pl.col("price_index") / pl.col("_pg_median")
    looks_entry_error = (ratio >= DATA_QUALITY_RATIO) | (ratio <= 1 / DATA_QUALITY_RATIO)
    return df.with_columns(
        (is_outlier & looks_entry_error).alias("flag_data_quality_anomaly"),
        (is_outlier & ~looks_entry_error).alias("flag_commercial_pricing_outlier"),
    ).drop("_absdev", "_pi_r", "_pair_med", "_resid", "_r_med")


def peer_group_stats(df: pl.DataFrame) -> pl.DataFrame:
    """Dispersion report per peer group: median, IQR, P10/P50/P90, discount stats."""
    return (
        df.group_by("peer_group", "peer_group_level")
        .agg(
            pl.len().alias("n_lines"),
            pl.col("price_index").median().alias("median"),
            (pl.col("price_index").quantile(0.75) - pl.col("price_index").quantile(0.25)).alias(
                "iqr"
            ),
            pl.col("price_index").quantile(0.10).alias("p10"),
            pl.col("price_index").quantile(0.50).alias("p50"),
            pl.col("price_index").quantile(0.90).alias("p90"),
            (1 - pl.col("price_index")).mean().alias("mean_discount_to_list"),
            (1 - pl.col("price_index")).quantile(0.90).alias("p90_discount_to_list"),
            pl.col("flag_data_quality_anomaly").sum().alias("n_data_quality"),
            pl.col("flag_commercial_pricing_outlier").sum().alias("n_commercial_outliers"),
        )
        .sort("peer_group")
    )


def abc_classification(observations: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Three separate ABC splits per item: revenue, units, order frequency."""
    a = rules.segmentation.abc_a_share
    b = rules.segmentation.abc_b_share
    # Stable input order so float sums (quantity_norm) and the ranking are
    # reproducible; ties on value break deterministically on item_id.
    observations = observations.sort("source_line_key")

    def _abc(metric: pl.Expr, name: str) -> pl.DataFrame:
        per_item = (
            observations.group_by("item_id")
            .agg(metric.alias("value"))
            .sort(["value", "item_id"], descending=[True, False])
            .with_columns((pl.col("value").cum_sum() / pl.col("value").sum()).alias("_cum"))
            .with_columns(
                pl.when(pl.col("_cum") <= a)
                .then(pl.lit("A"))
                .when(pl.col("_cum") <= a + b)
                .then(pl.lit("B"))
                .otherwise(pl.lit("C"))
                .alias(name)
            )
        )
        return per_item.select("item_id", name)

    rev = _abc(pl.col("net_amount_cents").sum(), "revenue_abc")
    units = _abc(pl.col("quantity_norm").sum(), "unit_abc")
    freq = _abc(pl.col("order_id").n_unique(), "frequency_abc")
    return rev.join(units, on="item_id").join(freq, on="item_id")


def run_dispersion(
    observations: pl.DataFrame, items: pl.DataFrame | None, rules: Rules
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Full Phase 3: returns (flagged observations, peer stats, abc table)."""
    # Stable row order before any float aggregation (quantiles, means over
    # price_index) so outputs are bit-identical across runs.
    df = observations.sort("source_line_key")
    if items is not None and "item_family" not in df.columns and "item_family" in items.columns:
        df = df.join(items.select("item_id", "item_family"), on="item_id", how="left")
    elif "item_family" not in df.columns:
        # An items table without a family column (minimal real exports): the
        # small-cohort family fallback is simply unavailable.
        df = df.with_columns(pl.lit(None, dtype=pl.String).alias("item_family"))
    df = normalized_price(df)
    df = assign_peer_groups(df, rules)
    df = flag_outliers(df, rules)
    stats = peer_group_stats(df)
    abc = abc_classification(observations, rules)
    df = df.join(abc, on="item_id", how="left")
    return df, stats, abc
