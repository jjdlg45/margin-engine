"""Pricing-law guardrails (Run 5, Phase 37).

Robinson-Patman review flag: two customers who are likely competitors of each
other (same values on the configured attributes, default industry + region)
receiving recommended prices for the SAME item that differ by more than the
configured threshold. The flag is a review prompt, not a conclusion: the
cost-to-serve difference between the two customers is shown beside it, because
cost justification is a recognized defense and the reviewer needs that number
first. Orientation, not legal advice — see docs/methodology.md.

The comparison uses each pair's recommended INVOICE price (target index x the
pair's effective list). Pairs without a usable list price cannot be compared
and are excluded with a note; nothing is inferred for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from margin_engine.config import Rules


@dataclass
class ComplianceResult:
    # One row per flagged competitor pair x item, with prices and the
    # cost-to-serve evidence.
    detail: pl.DataFrame
    # (customer_id, item_id, rp_review_flag) for every flagged guidance pair.
    flags: pl.DataFrame
    notes: list[str] = field(default_factory=list)


_DETAIL_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "item_id": pl.String,
    "customer_id_a": pl.String,
    "customer_id_b": pl.String,
    "recommended_price_a_cents": pl.Int64,
    "recommended_price_b_cents": pl.Int64,
    "price_difference_pct": pl.Float64,
    "cts_per_unit_a_cents": pl.Int64,
    "cts_per_unit_b_cents": pl.Int64,
    "cts_difference_cents": pl.Int64,
}


def _empty(notes: list[str]) -> ComplianceResult:
    return ComplianceResult(
        detail=pl.DataFrame(schema=_DETAIL_SCHEMA),
        flags=pl.DataFrame(
            schema={"customer_id": pl.String, "item_id": pl.String, "rp_review_flag": pl.Boolean}
        ),
        notes=notes,
    )


def run_robinson_patman(
    guidance: pl.DataFrame,
    customers: pl.DataFrame | None,
    cts_per_unit: pl.DataFrame | None,
    rules: Rules,
) -> ComplianceResult:
    cfg = rules.compliance.robinson_patman
    if not cfg.enabled:
        return _empty(["Robinson-Patman review flag OFF: disabled in config"])
    attrs = list(cfg.competitor_attributes)
    if customers is None:
        return _empty(
            [
                "Robinson-Patman review flag OFF: no customers reference table, "
                f"so the competitor definition ({' + '.join(attrs)}) cannot be evaluated"
            ]
        )
    missing = [a for a in attrs if a not in customers.columns]
    if missing:
        return _empty(
            [
                f"Robinson-Patman review flag OFF: customers table lacks "
                f"competitor attribute(s) {missing}"
            ]
        )
    notes: list[str] = []

    usable = guidance.filter(
        pl.col("target_index").is_not_null()
        & pl.col("eff_list_cents").is_not_null()
        & (pl.col("eff_list_cents") > 0)
    ).with_columns(
        (pl.col("target_index") * pl.col("eff_list_cents"))
        .round(0)
        .cast(pl.Int64)
        .alias("_rec_price_cents")
    )
    n_excluded = guidance.height - usable.height
    if n_excluded:
        notes.append(
            f"Robinson-Patman comparison: {n_excluded} guidance pair(s) have no "
            "usable list price and cannot be price-compared (excluded, not inferred)"
        )
    base = usable.select("customer_id", "item_id", "_rec_price_cents").join(
        customers.select("customer_id", *attrs), on="customer_id", how="left"
    )
    if cts_per_unit is not None:
        base = base.join(cts_per_unit, on=["customer_id", "item_id"], how="left")
    else:
        base = base.with_columns(pl.lit(None, dtype=pl.Int64).alias("cts_per_unit_cents"))

    # Competitor pairs: same item, same attribute values, two customers.
    keys = ["item_id", *attrs]
    left = base.rename(
        {
            "customer_id": "customer_id_a",
            "_rec_price_cents": "recommended_price_a_cents",
            "cts_per_unit_cents": "cts_per_unit_a_cents",
        }
    )
    right = base.rename(
        {
            "customer_id": "customer_id_b",
            "_rec_price_cents": "recommended_price_b_cents",
            "cts_per_unit_cents": "cts_per_unit_b_cents",
        }
    )
    pairs = left.join(right, on=keys, how="inner").filter(
        pl.col("customer_id_a") < pl.col("customer_id_b")
    )
    lo = pl.min_horizontal("recommended_price_a_cents", "recommended_price_b_cents")
    hi = pl.max_horizontal("recommended_price_a_cents", "recommended_price_b_cents")
    pairs = pairs.with_columns(
        pl.when(lo > 0).then((hi - lo) / lo * 100).otherwise(None).alias("price_difference_pct")
    )
    # Strictly greater than the threshold fires; the boundary itself does not.
    flagged = pairs.filter(
        pl.col("price_difference_pct").is_not_null()
        & (pl.col("price_difference_pct") > cfg.price_difference_threshold_pct)
    ).with_columns(
        (pl.col("cts_per_unit_a_cents") - pl.col("cts_per_unit_b_cents")).alias(
            "cts_difference_cents"
        )
    )
    detail = flagged.select(
        "item_id",
        "customer_id_a",
        "customer_id_b",
        "recommended_price_a_cents",
        "recommended_price_b_cents",
        pl.col("price_difference_pct").round(4),
        "cts_per_unit_a_cents",
        "cts_per_unit_b_cents",
        "cts_difference_cents",
    ).sort("item_id", "customer_id_a", "customer_id_b")
    flags = (
        pl.concat(
            [
                flagged.select(pl.col("customer_id_a").alias("customer_id"), "item_id"),
                flagged.select(pl.col("customer_id_b").alias("customer_id"), "item_id"),
            ]
        )
        .unique()
        .sort("customer_id", "item_id")
        .with_columns(pl.lit(value=True).alias("rp_review_flag"))
    )
    if detail.height:
        notes.append(
            f"Robinson-Patman review: {detail.height} competitor pair-item "
            f"combination(s) with recommended prices more than "
            f"{cfg.price_difference_threshold_pct:g}% apart (competitors = same "
            f"{' + '.join(attrs)}); cost-to-serve differences attached as "
            "supporting documentation. A review prompt, not legal advice."
        )
    return ComplianceResult(detail=detail, flags=flags, notes=notes)
