"""Opportunity sizing (Phase 7, pocket-aware since Run 4): three levels, never
summed across levels.

1. observed_leakage        reconciled dollars lost to discounts, rebates and
                           concessions over the baseline window (accounting
                           fact, not a promise).
2. gross_price_opportunity eligible quantity x positive POCKET gap to the
                           proposed price, at unchanged volume. The proposed
                           and current prices are INVOICE-basis (index x
                           effective list); the gap is converted to pocket
                           dollars with the pair's percentage post-invoice
                           deductions d, because a raised invoice price also
                           raises rebates and concessions that scale with it.
                           A conservative variant raises below-P25 pairs only
                           to P25 (24.13).
3. expected_contribution_change
                           contribution(P1) - contribution(P0) with unit
                           contribution = P x (1 - d) - F - C (pocket-aware,
                           24.1) and Q(P1) from the segment elasticity — only
                           where the elasticity status is
                           causal_elasticity_supported AND the proposed move
                           lies inside the segment's identifying price support
                           (24.6). At iv_estimated_elasticity the volume
                           response is a labeled sensitivity range; anything
                           lower gets fixed-volume numbers only.

Missing cost is never zero (24.2): pairs without a unit cost are excluded from
cost-dependent levels and flagged cost_unavailable; their level-2 numbers
(price-based, cost-free) remain.

Eligibility excludes contract-protected pairs and insufficient_data guidance.
Every record carries baseline window, action, constraints, quantity basis,
price assumptions, evidence status and a calculation trace string.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from margin_engine.config import Rules
from margin_engine.sensitivity.estimate import STATUS_CAUSAL, STATUS_IV

TREATMENT_EXPECTED = "expected_change"
TREATMENT_RANGE = "sensitivity_range_only"
TREATMENT_FIXED = "fixed_volume_only"
TREATMENT_OUTSIDE_SUPPORT = "outside_price_support"
TREATMENT_COST_UNAVAILABLE = "cost_unavailable"


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
        .sort(["total_leakage_cents", "customer_id"], descending=[True, False])
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

    sens_cols = [
        pl.col("segment").alias("segment_leaf"),
        pl.col("iv").alias("elasticity"),
        pl.col("iv_ci_low").alias("elasticity_ci_low"),
        pl.col("iv_ci_high").alias("elasticity_ci_high"),
        pl.col("status").alias("evidence_status"),
    ]
    # Identifying price support (24.6): the 5th-95th percentile range of
    # instrument-predicted within log-price movement per segment. Proposed
    # moves outside it are extrapolation and get no volume response.
    has_support = {"support_log_lo", "support_log_hi"}.issubset(sensitivity.columns)
    if has_support:
        sens_cols += [pl.col("support_log_lo"), pl.col("support_log_hi")]
    sens = sensitivity.select(sens_cols)
    g = g.join(sens, on="segment_leaf", how="left")
    if not has_support:
        g = g.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("support_log_lo"),
            pl.lit(None, dtype=pl.Float64).alias("support_log_hi"),
        )

    # Proposed INVOICE price: target index x effective list (both invoice
    # basis). Gap only if positive.
    g = g.with_columns(
        (pl.col("target_index") * pl.col("eff_list_cents")).alias("proposed_invoice_price_cents"),
        (pl.col("current_index") * pl.col("eff_list_cents")).alias("current_invoice_price_cents"),
    ).with_columns(
        (pl.col("proposed_invoice_price_cents") - pl.col("current_invoice_price_cents")).alias(
            "gap_invoice_cents"
        )
    )
    g = g.filter(pl.col("gap_invoice_cents") > 0)

    # Pocket retention factor (1 - d): share of an invoice dollar that reaches
    # pocket after percentage post-invoice deductions. F is per-unit and fixed,
    # so it cancels out of price GAPS but not of contribution LEVELS.
    if "deduction_bp" in g.columns:
        retain = (10_000 - pl.col("deduction_bp").fill_null(0)) / 10_000
    else:
        retain = pl.lit(1.0)
    fixed_f = (
        pl.col("fixed_deduction_cents").fill_null(0)
        if "fixed_deduction_cents" in g.columns
        else pl.lit(0)
    )
    g = g.with_columns(retain.alias("pocket_retention"), fixed_f.alias("fixed_deduction_f"))

    # Level 2: pocket dollars at unchanged volume.
    gross = (
        (pl.col("gap_invoice_cents") * pl.col("pocket_retention") * pl.col("baseline_qty"))
        .round(0)
        .alias("gross_price_opportunity_cents")
    )
    # Conservative variant (24.13): raise below-P25 pairs to P25 only.
    if "band_p25" in g.columns:
        p25_price = pl.col("band_p25") * pl.col("eff_list_cents")
        cons_gap = p25_price - pl.col("current_invoice_price_cents")
        conservative = (
            pl.when(cons_gap > 0)
            .then((cons_gap * pl.col("pocket_retention") * pl.col("baseline_qty")).round(0))
            .otherwise(0)
            .alias("gross_price_opportunity_conservative_cents")
        )
    else:
        conservative = pl.lit(None, dtype=pl.Int64).alias(
            "gross_price_opportunity_conservative_cents"
        )

    # Level 3: pocket-aware unit contribution c(P) = P(1-d) - F - C (24.1).
    # Missing cost is never zero (24.2): cost-dependent outputs stay null.
    unit_cost = pl.col("unit_cost_cents")
    has_cost = unit_cost.is_not_null()
    p0 = pl.col("current_invoice_price_cents")
    p1 = pl.col("proposed_invoice_price_cents")
    p_ratio = p1 / p0
    unit_c0 = p0 * pl.col("pocket_retention") - pl.col("fixed_deduction_f") - unit_cost
    unit_c1 = p1 * pl.col("pocket_retention") - pl.col("fixed_deduction_f") - unit_cost
    q1 = pl.col("baseline_qty") * p_ratio.pow(pl.col("elasticity"))
    contrib_0 = unit_c0 * pl.col("baseline_qty")
    delta = (unit_c1 * q1 - contrib_0).round(0)

    q1_lo = pl.col("baseline_qty") * p_ratio.pow(pl.col("elasticity_ci_low"))
    q1_hi = pl.col("baseline_qty") * p_ratio.pow(pl.col("elasticity_ci_high"))
    delta_lo = (unit_c1 * q1_lo - contrib_0).round(0)
    delta_hi = (unit_c1 * q1_hi - contrib_0).round(0)

    causal = pl.col("evidence_status") == STATUS_CAUSAL
    ivonly = pl.col("evidence_status") == STATUS_IV
    # Extrapolation guard (24.6): the proposed log-price move must lie inside
    # the segment's identifying support; outside it no volume response is
    # produced at any status.
    move = (p1 / p0).log()
    in_support = pl.col("support_log_lo").is_null() | (
        (move >= pl.col("support_log_lo")) & (move <= pl.col("support_log_hi"))
    )

    g = g.with_columns(
        gross,
        conservative,
        has_cost.not_().alias("cost_unavailable"),
        pl.when(causal & has_cost & in_support)
        .then(delta)
        .otherwise(None)
        .alias("expected_contribution_change_cents"),
        pl.when((causal | ivonly) & has_cost & in_support)
        .then(pl.min_horizontal(delta_lo, delta_hi))
        .otherwise(None)
        .alias("contribution_change_low_cents"),
        pl.when((causal | ivonly) & has_cost & in_support)
        .then(pl.max_horizontal(delta_lo, delta_hi))
        .otherwise(None)
        .alias("contribution_change_high_cents"),
        pl.when(has_cost.not_())
        .then(pl.lit(TREATMENT_COST_UNAVAILABLE))
        .when((causal | ivonly) & ~in_support)
        .then(pl.lit(TREATMENT_OUTSIDE_SUPPORT))
        .when(causal)
        .then(pl.lit(TREATMENT_EXPECTED))
        .when(ivonly)
        .then(pl.lit(TREATMENT_RANGE))
        .otherwise(pl.lit(TREATMENT_FIXED))
        .alias("volume_treatment"),
        pl.lit(f"last_{window}_months").alias("baseline_window"),
        pl.lit("raise_to_target_index").alias("action"),
        pl.lit("target >= floor; corridor P25-P75; contract-protected excluded").alias(
            "constraints"
        ),
        pl.lit("sum of panel qty over baseline window").alias("quantity_basis"),
        pl.format(
            "invoice gap = ({} - {}) x list {}c; pocket retention {}; qty {}",
            pl.col("target_index").round(4),
            pl.col("current_index").round(4),
            pl.col("eff_list_cents"),
            pl.col("pocket_retention").round(4),
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
        "current_invoice_price_cents",
        "proposed_invoice_price_cents",
        "gap_invoice_cents",
        "pocket_retention",
        "gross_price_opportunity_cents",
        "gross_price_opportunity_conservative_cents",
        "expected_contribution_change_cents",
        "contribution_change_low_cents",
        "contribution_change_high_cents",
        "cost_unavailable",
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
    """P* = argmax of pocket contribution (P(1-d) - F - C) x Q(P) for log-log
    demand, causal-supported only (24.1).

    First-order condition gives P* = (C + F) / (1 - d) x beta / (1 + beta) for
    beta < -1. Clipped to [floor, band P75]; for beta >= -1 the unconstrained
    optimum is unbounded, so the corridor top is used and labeled. Pairs
    without cost (24.2) or outside identifying price support (24.6) keep the
    provisional historical target. Everything else keeps provisional too.
    """
    g = guidance.select(
        "customer_id",
        "item_id",
        pl.col("unit_cost_cents"),
        pl.col("invoice_floor_cents"),
        pl.col("eff_list_cents"),
        pl.col("band_p75"),
        *(
            [pl.col("deduction_bp"), pl.col("fixed_deduction_cents")]
            if "deduction_bp" in guidance.columns
            else []
        ),
    )
    j = opportunities.join(g, on=["customer_id", "item_id"], how="left")
    if "deduction_bp" not in j.columns:
        j = j.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("deduction_bp"),
            pl.lit(0, dtype=pl.Int64).alias("fixed_deduction_cents"),
        )
    causal = pl.col("evidence_status") == STATUS_CAUSAL
    in_support = (
        pl.col("volume_treatment") != TREATMENT_OUTSIDE_SUPPORT
        if "volume_treatment" in j.columns
        else pl.lit(True)
    )
    if "elasticity" not in j.columns:
        return j.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("optimal_price_cents"),
            pl.lit("provisional_historical").alias("target_basis"),
        )
    beta = pl.col("elasticity")
    retain = (10_000 - pl.col("deduction_bp").fill_null(0)) / 10_000
    # Pocket-aware marginal cost of an invoice dollar: (C + F) / (1 - d).
    c_eff = (pl.col("unit_cost_cents") + pl.col("fixed_deduction_cents").fill_null(0)) / retain
    interior = causal & in_support & (beta < -1.0) & pl.col("unit_cost_cents").is_not_null()
    p_star = (c_eff * beta / (1.0 + beta)).round(0)
    corridor_hi = (pl.col("band_p75") * pl.col("eff_list_cents")).round(0)
    clipped = pl.min_horizontal(
        pl.max_horizontal(p_star, pl.col("invoice_floor_cents")), corridor_hi
    )
    return j.with_columns(
        pl.when(interior)
        .then(clipped)
        .when(causal & in_support & pl.col("unit_cost_cents").is_not_null())
        .then(corridor_hi)  # inelastic: corridor top, labeled below
        .otherwise(None)
        .cast(pl.Int64)
        .alias("optimal_price_cents"),
        pl.when(interior)
        .then(pl.lit("optimized_monopoly_markup_clipped"))
        .when(causal & in_support & pl.col("unit_cost_cents").is_not_null())
        .then(pl.lit("corridor_top_inelastic"))
        .otherwise(pl.lit("provisional_historical"))
        .alias("target_basis"),
    )
