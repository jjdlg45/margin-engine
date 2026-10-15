"""Price guidance (Phase 5): historical reference bands and economic floor.

Bands are P25/P50/P75 of the normalized price index within commercial segments,
on a configurable observation unit (default: one median price per customer x
item x month). Historical bands are NOT optimal prices and every target/start
is labeled `provisional_historical` until Phase 7 optimization is supported.

Economic floor (exact integer math, computed at FULL precision and rounded
exactly once at the end):
    pocket floor  = C / (1 - m)
    invoice floor = (C / (1 - m) + F) / (1 - d)
with C = unit product cost (cents), m = minimum pocket margin, F = fixed
per-unit post-invoice deductions (cents), d = percentage post-invoice
deductions (basis points).

Rounding is policy (price_guidance.floor_rounding): "ceiling" by default — a
floor exists to guarantee the minimum margin, and rounding it down would quietly
break that guarantee by a fraction of a cent; "half_even" is available for
deployments that prefer symmetric rounding and accept that property's loss.

Statuses: in_band, below_reference, below_floor, above_reference,
insufficient_pricing_headroom (corridor infeasible against the floor),
insufficient_data. Infeasible corridors are flagged, never relabeled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import polars as pl

from margin_engine.config import Rules
from margin_engine.money import div_half_even_expr

PROVISIONAL_LABEL = "provisional_historical"

STATUS_INSUFFICIENT_DATA = "insufficient_data"
STATUS_BELOW_FLOOR = "below_floor"
STATUS_BELOW_REFERENCE = "below_reference"
STATUS_IN_BAND = "in_band"
STATUS_ABOVE_REFERENCE = "above_reference"
STATUS_NO_HEADROOM = "insufficient_pricing_headroom"

FloorRounding = Literal["ceiling", "half_even"]


def _div_ceil_expr(numer: pl.Expr, denom: pl.Expr | int) -> pl.Expr:
    """Ceiling division for positive denominators, exact integer arithmetic."""
    return -((-numer).floordiv(denom))


def pocket_floor_cents(
    cost_cents: pl.Expr, min_margin_bp: int, rounding: FloorRounding = "ceiling"
) -> pl.Expr:
    """C / (1 - m) at full precision, rounded once. m in basis points."""
    numer = cost_cents * 10_000
    denom = 10_000 - min_margin_bp
    if rounding == "ceiling":
        return _div_ceil_expr(numer, denom)
    return div_half_even_expr(numer, denom)


def invoice_floor_cents(
    cost_cents: pl.Expr,
    min_margin_bp: int,
    fixed_deduction_cents: pl.Expr,
    deduction_bp: pl.Expr,
    rounding: FloorRounding = "ceiling",
) -> pl.Expr:
    """(C/(1-m) + F) / (1 - d) at FULL precision — no intermediate rounding of
    the pocket floor — rounded once at the end. d in basis points.

    Exact integer form:
        (C*10^8 + F*(10^4 - m)*10^4) / ((10^4 - m) * (10^4 - d))
    """
    m_den = 10_000 - min_margin_bp
    numer = cost_cents * 10_000 * 10_000 + fixed_deduction_cents * m_den * 10_000
    denom = m_den * (10_000 - deduction_bp)
    if rounding == "ceiling":
        return _div_ceil_expr(numer, denom)
    return div_half_even_expr(numer, denom)


@dataclass
class GuidanceResult:
    bands: pl.DataFrame  # per segment: p25/p50/p75, n_observations
    guidance: pl.DataFrame  # per customer x item: band, floor, status, target, start
    notes: list[str]


def band_observations(flagged: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Observation unit for bands. Data-quality anomalies are excluded (they are
    entry errors, not prices); commercial outliers stay (they happened)."""
    obs = flagged.filter(
        ~pl.col("flag_data_quality_anomaly")
        & pl.col("price_index").is_not_null()
        & pl.col("price_index").is_finite()
        & pl.col("segment_leaf").is_not_null()
    )
    if rules.price_guidance.observation_unit == "line":
        return obs.select("segment_leaf", "customer_id", "item_id", "price_index")
    return (
        obs.with_columns(pl.col("invoice_date").dt.strftime("%Y-%m").alias("_month"))
        .group_by("segment_leaf", "customer_id", "item_id", "_month")
        .agg(pl.col("price_index").median().alias("price_index"))
    )


def segment_bands(flagged: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    p_lo, p_mid, p_hi = rules.price_guidance.band_percentiles
    obs = band_observations(flagged, rules)
    return obs.group_by("segment_leaf").agg(
        pl.len().alias("n_observations"),
        pl.col("price_index").quantile(p_lo).alias("band_p25"),
        pl.col("price_index").quantile(p_mid).alias("band_p50"),
        pl.col("price_index").quantile(p_hi).alias("band_p75"),
    )


def build_guidance(
    flagged: pl.DataFrame,
    rules: Rules,
    rebate_programs: pl.DataFrame | None,
) -> GuidanceResult:
    """Per customer x item guidance against segment bands and economic floor."""
    pg = rules.price_guidance
    min_margin_bp = int(pg.min_pocket_margin * 10_000)
    notes: list[str] = []

    bands = segment_bands(flagged, rules)

    # Pair position: median index over the pair's history; unit economics from
    # the most recent sale line with known cost and list.
    base = flagged.filter(pl.col("price_index").is_not_null() & pl.col("price_index").is_finite())
    pair = (
        base.sort("invoice_date")
        .group_by("customer_id", "item_id")
        .agg(
            pl.col("segment_leaf").drop_nulls().last().alias("segment_leaf"),
            pl.col("price_index").median().alias("current_index"),
            pl.len().alias("n_lines"),
            pl.col("quantity_norm").sum().alias("total_qty"),
            # Latest known unit economics (cents, per normalized unit).
            (pl.col("product_cost_cents") / pl.col("quantity_norm"))
            .drop_nulls()
            .last()
            .alias("unit_cost_f"),
            pl.col("eff_list_cents").drop_nulls().last().alias("eff_list_cents"),
            pl.col("contract_id").drop_nulls().last().alias("contract_id"),
            (
                pl.col("item_role").drop_nulls().first()
                if "item_role" in base.columns
                else pl.lit(None, dtype=pl.String)
            ).alias("item_role"),
        )
        .with_columns(pl.col("unit_cost_f").round(0).cast(pl.Int64).alias("unit_cost_cents"))
        .drop("unit_cost_f")
    )

    # Percentage post-invoice deductions d: the customer's rebate program rate
    # plus the customer's realized freight-concession rate (concessions / net
    # on sale lines), both in basis points. Both reduce pocket revenue, so both
    # belong in the invoice floor (spec 5.5 / Phase 5).
    if rebate_programs is not None:
        rp = rebate_programs.with_columns(
            (pl.col("rate_pct") * 100).round(0).cast(pl.Int64).alias("rebate_bp")
        )
        pair = pair.join(
            rp.select("customer_id", "rebate_bp"), on="customer_id", how="left"
        ).with_columns(pl.col("rebate_bp").fill_null(0))
    else:
        pair = pair.with_columns(pl.lit(0, dtype=pl.Int64).alias("rebate_bp"))
        notes.append("no rebate_programs reference: floor assumes zero rebate deduction")
    if "freight_concession_cents" in base.columns:
        conc = (
            base.filter(pl.col("net_amount_cents") > 0)
            .group_by("customer_id")
            .agg(
                (
                    pl.col("freight_concession_cents").sum()
                    * 10_000
                    // pl.col("net_amount_cents").sum()
                ).alias("concession_bp")
            )
        )
        pair = pair.join(conc, on="customer_id", how="left").with_columns(
            pl.col("concession_bp").fill_null(0)
        )
    else:
        pair = pair.with_columns(pl.lit(0, dtype=pl.Int64).alias("concession_bp"))
    pair = pair.with_columns(
        (pl.col("rebate_bp") + pl.col("concession_bp")).alias("deduction_bp"),
        pl.lit(0, dtype=pl.Int64).alias("fixed_deduction_cents"),
    )

    rounding = pg.floor_rounding
    pair = pair.with_columns(
        pocket_floor_cents(pl.col("unit_cost_cents"), min_margin_bp, rounding).alias(
            "pocket_floor_cents"
        ),
        invoice_floor_cents(
            pl.col("unit_cost_cents"),
            min_margin_bp,
            pl.col("fixed_deduction_cents"),
            pl.col("deduction_bp"),
            rounding,
        ).alias("invoice_floor_cents"),
    ).with_columns(
        pl.when(pl.col("eff_list_cents") > 0)
        .then(pl.col("invoice_floor_cents") / pl.col("eff_list_cents"))
        .otherwise(None)
        .alias("floor_index")
    )

    g = pair.join(bands, on="segment_leaf", how="left")

    # Role-specific corridor policy (Phase 15): KVI corridors are pulled toward
    # the segment median — key-value items get less pricing latitude.
    shrink = float(pg.kvi_corridor_shrink)
    is_kvi = pl.col("item_role") == "kvi"
    if shrink < 1.0:
        g = g.with_columns(
            pl.when(is_kvi)
            .then(pl.col("band_p50") - (pl.col("band_p50") - pl.col("band_p25")) * shrink)
            .otherwise(pl.col("band_p25"))
            .alias("band_p25"),
            pl.when(is_kvi)
            .then(pl.col("band_p50") + (pl.col("band_p75") - pl.col("band_p50")) * shrink)
            .otherwise(pl.col("band_p75"))
            .alias("band_p75"),
            pl.when(is_kvi)
            .then(pl.lit("kvi_tightened_corridor"))
            .otherwise(pl.lit(None, dtype=pl.String))
            .alias("corridor_policy"),
        )
    else:
        g = g.with_columns(pl.lit(None, dtype=pl.String).alias("corridor_policy"))

    insufficient = (
        pl.col("n_observations").is_null()
        | (pl.col("n_observations") < pg.min_band_observations)
        | pl.col("current_index").is_null()
    )
    # Floor comparisons need cost and list; without them only band statuses apply.
    has_floor = pl.col("floor_index").is_not_null() & pl.col("unit_cost_cents").is_not_null()
    no_headroom = has_floor & (pl.col("floor_index") > pl.col("band_p75"))
    below_floor = has_floor & (pl.col("current_index") < pl.col("floor_index"))

    g = g.with_columns(
        pl.when(insufficient)
        .then(pl.lit(STATUS_INSUFFICIENT_DATA))
        .when(no_headroom)
        .then(pl.lit(STATUS_NO_HEADROOM))
        .when(below_floor)
        .then(pl.lit(STATUS_BELOW_FLOOR))
        .when(pl.col("current_index") < pl.col("band_p25"))
        .then(pl.lit(STATUS_BELOW_REFERENCE))
        .when(pl.col("current_index") > pl.col("band_p75"))
        .then(pl.lit(STATUS_ABOVE_REFERENCE))
        .otherwise(pl.lit(STATUS_IN_BAND))
        .alias("guidance_status")
    )

    # Provisional target/start: P50/P75 of the band, never below the floor.
    # No target is produced where data is insufficient or the corridor is
    # infeasible — infeasibility is flagged, not papered over.
    produce = ~insufficient & ~no_headroom
    g = g.with_columns(
        pl.when(produce & has_floor)
        .then(pl.max_horizontal("band_p50", "floor_index"))
        .when(produce)
        .then(pl.col("band_p50"))
        .otherwise(None)
        .alias("target_index"),
        pl.when(produce & has_floor)
        .then(pl.max_horizontal("band_p75", "floor_index"))
        .when(produce)
        .then(pl.col("band_p75"))
        .otherwise(None)
        .alias("start_index"),
        pl.lit(PROVISIONAL_LABEL).alias("target_label"),
        pl.col("contract_id").is_not_null().alias("contract_protected"),
    )
    return GuidanceResult(bands=bands, guidance=g, notes=notes)
