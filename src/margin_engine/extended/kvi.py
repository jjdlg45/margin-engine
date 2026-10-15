"""Product roles and key-value-item candidates (Phase 15).

The data-driven KVI candidate score combines three percentile ranks:
purchase frequency (distinct orders), customer breadth (distinct customers)
and a price-comparability proxy (LOW within-item price dispersion — a price
customers see often and compare easily). The output is labeled CANDIDATES:
a score, not a decision; the configured roles from the item master remain the
operative roles.
"""

from __future__ import annotations

import polars as pl

from margin_engine.config import Rules


def kvi_candidate_scores(flagged_observations: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """One row per item: components, score, and is_kvi_candidate for the top
    configured share."""
    per_item = (
        flagged_observations.filter(pl.col("price_index").is_not_null())
        .group_by("item_id")
        .agg(
            pl.col("order_id").n_unique().alias("order_frequency"),
            pl.col("customer_id").n_unique().alias("customer_breadth"),
            (pl.col("price_index").quantile(0.75) - pl.col("price_index").quantile(0.25)).alias(
                "price_iqr"
            ),
            pl.len().alias("n_lines"),
        )
    )
    n = per_item.height
    per_item = per_item.with_columns(
        (pl.col("order_frequency").rank("average") / n).alias("_r_freq"),
        (pl.col("customer_breadth").rank("average") / n).alias("_r_breadth"),
        # Comparability: tighter dispersion ranks higher.
        (1 - pl.col("price_iqr").rank("average") / n).alias("_r_comparability"),
    ).with_columns(
        # Frequency and breadth are the primary KVI signals; comparability is a
        # weaker proxy and enters at half weight.
        ((pl.col("_r_freq") + pl.col("_r_breadth") + 0.5 * pl.col("_r_comparability")) / 2.5).alias(
            "kvi_score"
        )
    )
    cutoff = per_item["kvi_score"].quantile(1 - rules.price_guidance.kvi_candidate_share)
    return (
        per_item.with_columns(
            (pl.col("kvi_score") >= cutoff).alias("is_kvi_candidate"),
            pl.lit("candidate_score_not_decision").alias("kvi_label"),
        )
        .drop("_r_freq", "_r_breadth", "_r_comparability")
        .sort("kvi_score", descending=True)
    )
