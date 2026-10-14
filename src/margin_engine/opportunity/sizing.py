"""Opportunity sizing (Phase 7): three levels, never summed across levels.

1. observed_leakage        reconciled dollars lost to discounts, rebates and
                           concessions over the baseline window (accounting
                           fact, not a promise).
2. gross_price_opportunity eligible quantity x positive gap to the proposed
                           pocket price, at unchanged volume.
3. expected_contribution_change
                           (P1-C1)Q(P1) - (P0-C0)Q(P0) with Q(P1) from the
                           segment elasticity — only where the elasticity
                           status is causal_elasticity_supported. At
                           iv_estimated_elasticity the volume response is shown
                           as a labeled sensitivity range; anything lower gets
                           fixed-volume numbers only.

Eligibility excludes contract-protected pairs and insufficient_data guidance.
Every record carries baseline window, action, constraints, quantity basis,
price assumptions, evidence status and a calculation trace string.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from margin_engine.config import Rules
from margin_engine.sensitivity.estimate import STATUS_CAUSAL, STATUS_IV


@dataclass
class OpportunityResult:
    leakage: pl.DataFrame
    opportunities: pl.DataFrame
    notes: list[str]


def observed_leakage(ledger: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Level 1: where the dollars went, reconciled. Baseline window only."""
    window = rules.opportunity.baseline_window_months
    max_date = ledger["invoice_date"].max()
    cutoff = pl.lit(max_date).dt.offset_by(f"-{window}mo")
    sales = ledger.filter(
        (pl.col("transaction_type") == "sale") & (pl.col("invoice_date") >= cutoff)
    )
    return (
        sales.group_by("customer_id")
        .agg(
            pl.col("net_amount_cents").sum().alias("net_revenue_cents"),
            pl.col("discount_cents").clip(lower_bound=0).sum().alias("on_invoice_discount_cents"),
            pl.col("rebate_actual_cents").sum().alias("rebate_actual_cents"),
            pl.col("rebate_settlement_alloc_cents").sum().alias("rebate_settlement_cents"),
            pl.col("freight_concession_cents").sum().alias("freight_concession_cents"),
            (-pl.col("price_credit_alloc_cents").sum()).alias("price_credits_cents"),
        )
        .with_columns(
            (
                pl.col("on_invoice_discount_cents")
                + pl.col("rebate_actual_cents")
                + pl.col("rebate_settlement_cents")
                + pl.col("freight_concession_cents")
                + pl.col("price_credits_cents")
            ).alias("total_leakage_cents")
        )
        .sort("total_leakage_cents", descending=True)
    )


def price_opportunities(
    guidance: pl.DataFrame,
    panel: pl.DataFrame,
    sensitivity: pl.DataFrame,
    rules: Rules,
) -> pl.DataFrame:
    """Levels 2 and 3 per customer x item, from guidance targets."""
    oc = rules.opportunity
    window = rules.opportunity.baseline_window_months
    max_m = int(panel["month_idx"].max() or 0)  # type: ignore[arg-type]
    base = (
        panel.filter((pl.col("qty") > 0) & (pl.col("month_idx") > max_m - window))
        .group_by("customer_id", "item_id")
        .agg(
            pl.col("qty").sum().alias("baseline_qty"),
            (pl.col("price_cf") * pl.col("qty")).sum().alias("baseline_rev_weighted"),
        )
        .with_columns(
            (pl.col("baseline_rev_weighted") / pl.col("baseline_qty")).alias("baseline_price_cents")
        )
    )
    g = guidance.join(base, on=["customer_id", "item_id"], how="inner")

    eligible = pl.col("target_index").is_not_null() & (
        pl.col("guidance_status") != "insufficient_data"
    )
    if oc.exclude_contract_protected:
        eligible = eligible & ~pl.col("contract_protected")
    g = g.filter(eligible)

    sens = sensitivity.select(
        pl.col("segment").alias("segment_leaf"),
        pl.col("iv").alias("elasticity"),
        pl.col("iv_ci_low").alias("elasticity_ci_low"),
        pl.col("iv_ci_high").alias("elasticity_ci_high"),
        pl.col("status").alias("evidence_status"),
    )
    g = g.join(sens, on="segment_leaf", how="left")

    # Proposed pocket price: target index x effective list. Gap only if positive.
    g = g.with_columns(
        (pl.col("target_index") * pl.col("eff_list_cents")).alias("proposed_price_cents"),
        (pl.col("current_index") * pl.col("eff_list_cents")).alias("current_price_cents"),
    ).with_columns(
        (pl.col("proposed_price_cents") - pl.col("current_price_cents")).alias("gap_cents")
    )
    g = g.filter(pl.col("gap_cents") > 0)

    gross = (
        (pl.col("gap_cents") * pl.col("baseline_qty"))
        .round(0)
        .alias("gross_price_opportunity_cents")
    )

    # Volume response: Q1 = Q0 * (P1/P0)^beta, causal only; IV status -> range.
    p_ratio = pl.col("proposed_price_cents") / pl.col("current_price_cents")
    unit_cost = (pl.col("unit_cost_cents")).fill_null(0)
    q1 = pl.col("baseline_qty") * p_ratio.pow(pl.col("elasticity"))
    contrib_0 = (pl.col("current_price_cents") - unit_cost) * pl.col("baseline_qty")
    contrib_1 = (pl.col("proposed_price_cents") - unit_cost) * q1
    delta = (contrib_1 - contrib_0).round(0)

    q1_lo = pl.col("baseline_qty") * p_ratio.pow(pl.col("elasticity_ci_low"))
    q1_hi = pl.col("baseline_qty") * p_ratio.pow(pl.col("elasticity_ci_high"))
    delta_lo = ((pl.col("proposed_price_cents") - unit_cost) * q1_lo - contrib_0).round(0)
    delta_hi = ((pl.col("proposed_price_cents") - unit_cost) * q1_hi - contrib_0).round(0)

    causal = pl.col("evidence_status") == STATUS_CAUSAL
    ivonly = pl.col("evidence_status") == STATUS_IV

    g = g.with_columns(
        gross,
        pl.when(causal).then(delta).otherwise(None).alias("expected_contribution_change_cents"),
        pl.when(causal | ivonly)
        .then(pl.min_horizontal(delta_lo, delta_hi))
        .otherwise(None)
        .alias("contribution_change_low_cents"),
        pl.when(causal | ivonly)
        .then(pl.max_horizontal(delta_lo, delta_hi))
        .otherwise(None)
        .alias("contribution_change_high_cents"),
        pl.when(causal)
        .then(pl.lit("expected_change"))
        .when(ivonly)
        .then(pl.lit("sensitivity_range_only"))
        .otherwise(pl.lit("fixed_volume_only"))
        .alias("volume_treatment"),
        pl.lit(f"last_{window}_months").alias("baseline_window"),
        pl.lit("raise_to_target_index").alias("action"),
        pl.lit("target >= floor; corridor P25-P75; contract-protected excluded").alias(
            "constraints"
        ),
        pl.lit("sum of panel qty over baseline window").alias("quantity_basis"),
        pl.format(
            "gap = ({} - {}) x list {}c; qty {}",
            pl.col("target_index").round(4),
            pl.col("current_index").round(4),
            pl.col("eff_list_cents"),
            pl.col("baseline_qty"),
        ).alias("calc_trace"),
    )
    return g.select(
        "customer_id",
        "item_id",
        "segment_leaf",
        "guidance_status",
        "evidence_status",
        "baseline_qty",
        "current_price_cents",
        "proposed_price_cents",
        "gap_cents",
        "gross_price_opportunity_cents",
        "expected_contribution_change_cents",
        "contribution_change_low_cents",
        "contribution_change_high_cents",
        "volume_treatment",
        "baseline_window",
        "action",
        "constraints",
        "quantity_basis",
        "calc_trace",
    )


def optimize_targets(
    opportunities: pl.DataFrame, guidance: pl.DataFrame, rules: Rules
) -> pl.DataFrame:
    """P* = argmax (P - C_var) Q(P) for log-log demand, causal-supported only.

    For beta < -1: P* = C_var * beta / (1 + beta). Clipped to [floor, band P75];
    for beta >= -1 the unconstrained optimum is unbounded, so the corridor top
    is used and labeled. Everything else keeps the provisional historical target.
    """
    g = guidance.select(
        "customer_id",
        "item_id",
        pl.col("unit_cost_cents"),
        pl.col("invoice_floor_cents"),
        pl.col("eff_list_cents"),
        pl.col("band_p75"),
    )
    j = opportunities.join(g, on=["customer_id", "item_id"], how="left")
    causal = pl.col("evidence_status") == STATUS_CAUSAL
    beta = pl.col("elasticity") if "elasticity" in j.columns else pl.lit(None)
    if "elasticity" not in j.columns:
        return j.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("optimal_price_cents"),
            pl.lit("provisional_historical").alias("target_basis"),
        )
    interior = causal & (beta < -1.0) & pl.col("unit_cost_cents").is_not_null()
    p_star = (pl.col("unit_cost_cents") * beta / (1.0 + beta)).round(0)
    corridor_hi = (pl.col("band_p75") * pl.col("eff_list_cents")).round(0)
    clipped = pl.min_horizontal(
        pl.max_horizontal(p_star, pl.col("invoice_floor_cents")), corridor_hi
    )
    return j.with_columns(
        pl.when(interior)
        .then(clipped)
        .when(causal)
        .then(corridor_hi)  # inelastic: corridor top, labeled below
        .otherwise(None)
        .cast(pl.Int64)
        .alias("optimal_price_cents"),
        pl.when(interior)
        .then(pl.lit("optimized_monopoly_markup_clipped"))
        .when(causal)
        .then(pl.lit("corridor_top_inelastic"))
        .otherwise(pl.lit("provisional_historical"))
        .alias("target_basis"),
    )
