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
from margin_engine.fiscal import FiscalCalendar
from margin_engine.opportunity.deductions import (
    TREATMENT_APPROX,
    TREATMENT_FLAT,
    TREATMENT_RECOMPUTED,
    TieredDeductionContext,
)
from margin_engine.sensitivity.estimate import (
    STATUS_CAUSAL,
    STATUS_IV,
    parse_support_regions,
)
from margin_engine.sensitivity.market_control import PATH_CONTROLLED, PATH_NO_PATH

TREATMENT_EXPECTED = "expected_change"
TREATMENT_RANGE = "sensitivity_range_only"
TREATMENT_FIXED = "fixed_volume_only"
TREATMENT_OUTSIDE_SUPPORT = "outside_price_support"
TREATMENT_COST_UNAVAILABLE = "cost_unavailable"
# The segment's elasticity answers a market-wide question, so no volume
# response is applied to a unilateral move (Run 6, Phase 70).
TREATMENT_PATH_UNCONTROLLED = "competitor_path_uncontrolled"


@dataclass
class OpportunityResult:
    leakage: pl.DataFrame
    opportunities: pl.DataFrame
    notes: list[str]


def observed_leakage(
    ledger: pl.DataFrame, rules: Rules, fiscal: FiscalCalendar | None = None
) -> pl.DataFrame:
    """Level 1: where the dollars went, reconciled. Baseline window only.

    Default calendar: the historical rolling window (max date minus N calendar
    months). A fiscal calendar (Phase 36) uses the last N fiscal periods
    instead, aligned to period boundaries."""
    window = rules.opportunity.baseline_window_months
    if fiscal is None or fiscal.is_calendar_months:
        max_date = ledger["invoice_date"].max()
        cutoff = pl.lit(max_date).dt.offset_by(f"-{window}mo")
        sales = ledger.filter(
            (pl.col("transaction_type") == "sale") & (pl.col("invoice_date") >= cutoff)
        )
    else:
        sales = fiscal.attach(
            ledger.filter(pl.col("transaction_type") == "sale"), "invoice_date", idx="_fp_idx"
        )
        max_idx = int(sales["_fp_idx"].max() or 0)  # type: ignore[arg-type]
        sales = sales.filter(pl.col("_fp_idx") > max_idx - window).drop("_fp_idx")
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
    *,
    tiered_ctx: TieredDeductionContext | None = None,
    tiered_customers: set[str] | None = None,
) -> pl.DataFrame:
    """Levels 2 and 3 per customer x item, from guidance targets.

    Deduction treatment (Run 6, Phase 66): flat percentage rates on the same
    base (net invoiced value) flatten into d exactly; TIERED programs are
    RECOMPUTED at the candidate revenue via `tiered_ctx` (crossing a tier
    re-rates the whole basis), and a tiered customer WITHOUT a tier table gets
    the (1 - d) result labeled a fixed-rate approximation - stated, never
    silent. Every record carries `deduction_treatment`."""
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

    # Casts: when NO segment has an IV estimate these columns are all null and
    # polars infers dtype Null, on which pow/abs raise (27.7 matrix finding).
    sens_cols = [
        pl.col("segment").alias("segment_leaf"),
        pl.col("iv").cast(pl.Float64).alias("elasticity"),
        pl.col("iv_ci_low").cast(pl.Float64).alias("elasticity_ci_low"),
        pl.col("iv_ci_high").cast(pl.Float64).alias("elasticity_ci_high"),
        pl.col("status").alias("evidence_status"),
    ]
    # Competitor-price path (Run 6, Phase 70): a market-wide estimand's
    # interval is not a confidence interval for the UNILATERAL move being
    # priced, and its bias direction is known and adverse (|beta| understated
    # -> volume loss understated -> expected gain overstated). So a segment
    # whose competitor-price path is uncontrolled gets no two-sided range.
    has_path = "competitor_path_resolution" in sensitivity.columns
    if has_path:
        sens_cols.append(pl.col("competitor_path_resolution"))
    if "estimand" in sensitivity.columns:
        sens_cols.append(pl.col("estimand").alias("elasticity_estimand"))
    # Identification support (24.6, refined in Run 6 Phase 74): the stretches
    # of within log-price movement the segment's instruments actually induced,
    # densely enough to inform a response. Proposed moves outside them are
    # extrapolation and get no volume response.
    has_support = {"support_log_lo", "support_log_hi"}.issubset(sensitivity.columns)
    if has_support:
        sens_cols += [pl.col("support_log_lo"), pl.col("support_log_hi")]
    has_regions = "support_regions" in sensitivity.columns
    if has_regions:
        sens_cols.append(pl.col("support_regions"))
    # Total-quantity elasticity (Run 6, Phase 72). The elasticity priced above
    # is the CONDITIONAL one, estimated on months a customer bought, so a
    # forecast built on it holds buying incidence fixed - it answers "they buy
    # less per order", never "some of them stop buying". The total-quantity
    # estimate is the one that counts the stopping, and it is carried here so
    # the conditional number is never the only volume response on the record.
    # It is a SECOND figure next to the first, never a replacement and never
    # added to it: its ladder has no causal tier, so nothing prices from it.
    has_total = "iv_ppml" in sensitivity.columns
    if has_total:
        sens_cols += [
            pl.col("iv_ppml").cast(pl.Float64).alias("total_quantity_elasticity"),
            pl.col("iv_ppml_status").alias("total_quantity_status"),
        ]
    sens = sensitivity.select(sens_cols)
    g = g.join(sens, on="segment_leaf", how="left")
    if not has_total:
        g = g.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("total_quantity_elasticity"),
            pl.lit(None, dtype=pl.String).alias("total_quantity_status"),
        )
    if not has_support:
        g = g.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("support_log_lo"),
            pl.lit(None, dtype=pl.Float64).alias("support_log_hi"),
        )
    if not has_regions:
        g = g.with_columns(pl.lit(None, dtype=pl.String).alias("support_regions"))
    if not has_path:
        g = g.with_columns(pl.lit(None, dtype=pl.String).alias("competitor_path_resolution"))
    if "elasticity_estimand" not in g.columns:
        g = g.with_columns(pl.lit(None, dtype=pl.String).alias("elasticity_estimand"))

    # Proposed INVOICE price: target index x the index DENOMINATOR. With a
    # valid effective list that is the list price; without one (real exports
    # with no price list, 24.4/28.2) the denominator is recovered from the
    # pair's own baseline price and its index (baseline / current_index), so
    # the gap stays index-consistent — labeled with its provenance.
    has_list = pl.col("eff_list_cents").is_not_null() & (pl.col("eff_list_cents") > 0)
    denom = (
        pl.when(has_list)
        .then(pl.col("eff_list_cents"))
        .when(pl.col("current_index") > 0)
        .then(pl.col("baseline_price_cents") / pl.col("current_index"))
        .otherwise(None)
    )
    g = g.with_columns(
        (pl.col("target_index") * denom).alias("proposed_invoice_price_cents"),
        (pl.col("current_index") * denom).alias("current_invoice_price_cents"),
        pl.when(has_list)
        .then(pl.lit("effective_list"))
        .otherwise(pl.lit("index_fallback_item_median"))
        .alias("price_basis"),
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
        # round(6): deduction_bp is an integer, so retention has at most 4
        # decimal digits - the round is lossless and pins the value against
        # polars choosing a divide vs reciprocal-multiply kernel between
        # otherwise identical runs (a last-ulp difference the 27.7 matrix
        # determinism invariant caught in opportunities.parquet).
        retain = ((10_000 - pl.col("deduction_bp").fill_null(0)) / 10_000).round(6)
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
        p25_price = pl.col("band_p25") * denom
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

    # Level 3 (Run 6, Phase 65): TWO expected-change bases, never conflated.
    # Pocket gross profit basis: c(P) = P(1-d) - F - C (24.1). Contribution
    # basis additionally subtracts the pair's per-unit cost-to-serve K
    # (estimated from activity rates, labeled); without a supported K the
    # contribution figures are unavailable, never borrowed from the GP basis.
    # Missing cost is never zero (24.2): cost-dependent outputs stay null.
    unit_cost = pl.col("unit_cost_cents")
    has_cost = unit_cost.is_not_null()
    k_unit = (
        pl.col("cts_per_unit_cents").cast(pl.Float64)
        if "cts_per_unit_cents" in g.columns
        else pl.lit(None, dtype=pl.Float64)
    )
    g = g.with_columns(
        k_unit.alias("k_per_unit_cents"),
        pl.when(k_unit.is_not_null())
        .then(pl.lit("estimated"))
        .otherwise(pl.lit("unavailable"))
        .alias("k_status"),
    )
    has_k = pl.col("k_per_unit_cents").is_not_null()
    p0 = pl.col("current_invoice_price_cents")
    p1 = pl.col("proposed_invoice_price_cents")
    p_ratio = p1 / p0
    unit_c0 = p0 * pl.col("pocket_retention") - pl.col("fixed_deduction_f") - unit_cost
    unit_c1 = p1 * pl.col("pocket_retention") - pl.col("fixed_deduction_f") - unit_cost
    q0 = pl.col("baseline_qty")
    q1 = q0 * p_ratio.pow(pl.col("elasticity"))
    delta = (unit_c1 * q1 - unit_c0 * q0).round(0)
    # Contribution basis: subtract K per unit on both sides.
    kc = pl.col("k_per_unit_cents").fill_null(0)
    delta_contrib = ((unit_c1 - kc) * q1 - (unit_c0 - kc) * q0).round(0)

    # The same arithmetic under the total-quantity elasticity: what the figure
    # would be if the extensive margin moved as the zeros-inclusive estimate
    # says it does. Reported beside the headline as the sensitivity to the
    # assumption the headline makes, never summed with it and never priced.
    q1_total = q0 * p_ratio.pow(pl.col("total_quantity_elasticity"))
    delta_total = (unit_c1 * q1_total - unit_c0 * q0).round(0)

    q1_lo = q0 * p_ratio.pow(pl.col("elasticity_ci_low"))
    q1_hi = q0 * p_ratio.pow(pl.col("elasticity_ci_high"))
    delta_lo = (unit_c1 * q1_lo - unit_c0 * q0).round(0)
    delta_hi = (unit_c1 * q1_hi - unit_c0 * q0).round(0)
    delta_contrib_lo = ((unit_c1 - kc) * q1_lo - (unit_c0 - kc) * q0).round(0)
    delta_contrib_hi = ((unit_c1 - kc) * q1_hi - (unit_c0 - kc) * q0).round(0)

    causal = pl.col("evidence_status") == STATUS_CAUSAL
    ivonly = pl.col("evidence_status") == STATUS_IV
    # A range is only shown when the interval belongs to the estimand being
    # priced: a resolved competitor-price path, or no path information at all
    # (older sensitivity frames, where the engine says nothing either way).
    path_ok = pl.col("competitor_path_resolution").is_null() | pl.col(
        "competitor_path_resolution"
    ).is_in([PATH_CONTROLLED, PATH_NO_PATH])
    # Identification-support rule (24.6, refined in Run 6 Phase 74). The
    # proposed log-price move must land where the segment's instruments
    # actually moved price, densely enough to say anything about the response;
    # outside that, no volume response is produced at any status.
    #
    # The test is against REGIONS rather than a single outer range, because
    # support is not uniform between the outer bounds. A segment whose
    # instrument-induced movement clusters around -6% and +4%, with nothing in
    # between, supports neither a 0% nor a -1% move however wide its outer
    # bounds look. Each region is a stretch where enough movements sit within
    # the configured window; a segment whose support is one connected stretch
    # produces one region and behaves exactly as before.
    move = (p1 / p0).log()
    regions_by_segment = {
        row["segment_leaf"]: parse_support_regions(row["support_regions"] or "")
        for row in g.select("segment_leaf", "support_regions").unique().to_dicts()
    }
    max_regions = max((len(r) for r in regions_by_segment.values()), default=0)
    if max_regions:
        # Widen the regions into fixed columns so the test stays a vectorized
        # expression: segments have at most a handful of them.
        region_cols: list[pl.Expr] = []
        for k in range(max_regions):
            lo_map = {
                seg: (r[k][0] if len(r) > k else None) for seg, r in regions_by_segment.items()
            }
            hi_map = {
                seg: (r[k][1] if len(r) > k else None) for seg, r in regions_by_segment.items()
            }
            region_cols += [
                pl.col("segment_leaf")
                .replace_strict(lo_map, default=None, return_dtype=pl.Float64)
                .alias(f"_sup_lo_{k}"),
                pl.col("segment_leaf")
                .replace_strict(hi_map, default=None, return_dtype=pl.Float64)
                .alias(f"_sup_hi_{k}"),
            ]
        g = g.with_columns(region_cols)
        in_regions = pl.any_horizontal(
            *[
                (move >= pl.col(f"_sup_lo_{k}")) & (move <= pl.col(f"_sup_hi_{k}"))
                for k in range(max_regions)
            ]
        ).fill_null(False)
        # How far beyond the nearest supported stretch the proposal reaches,
        # in log-price units. Zero inside; reported so a reader can see
        # whether a refusal was marginal or nowhere near.
        region_gap = pl.min_horizontal(
            *[
                pl.max_horizontal(
                    pl.col(f"_sup_lo_{k}") - move, move - pl.col(f"_sup_hi_{k}"), pl.lit(0.0)
                )
                for k in range(max_regions)
            ]
        )
    else:
        in_regions = pl.lit(False)
        region_gap = pl.lit(None, dtype=pl.Float64)

    # Three cases, in order. A segment whose regions were measured is held to
    # them. A segment carrying only the outer bounds comes from a sensitivity
    # frame written before the regions existed, and is held to the bounds, as
    # it was then. A segment with neither was never measured against this rule
    # at all - no instruments, or an older frame - and is not refused by it;
    # whatever evidence status it carries is what limits it.
    measured_regions = pl.col("support_regions").is_not_null()
    has_bounds = pl.col("support_log_lo").is_not_null()
    in_bounds = (move >= pl.col("support_log_lo")) & (move <= pl.col("support_log_hi"))
    bounds_gap = pl.max_horizontal(
        pl.col("support_log_lo") - move, move - pl.col("support_log_hi"), pl.lit(0.0)
    )
    in_support = (
        pl.when(measured_regions)
        .then(in_regions)
        .when(has_bounds)
        .then(in_bounds.fill_null(False))
        .otherwise(True)
    )
    g = g.with_columns(
        pl.when(measured_regions)
        .then(region_gap)
        .when(has_bounds)
        .then(bounds_gap)
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("price_support_distance"),
        pl.when(in_support)
        .then(
            pl.when(measured_regions | has_bounds)
            .then(pl.lit("inside the movement the instruments induced"))
            .otherwise(pl.lit("not measured for this segment"))
        )
        .when(measured_regions & pl.lit(max_regions == 0))
        .then(pl.lit("the instruments induced no densely supported movement in this segment"))
        .otherwise(pl.lit("beyond the movement the instruments induced"))
        .alias("price_support_reason"),
    )

    g = g.with_columns(
        gross,
        conservative,
        has_cost.not_().alias("cost_unavailable"),
        pl.when(causal & has_cost & in_support)
        .then(delta)
        .otherwise(None)
        .alias("expected_pocket_gross_profit_change_cents"),
        pl.when((causal | ivonly) & has_cost & in_support & path_ok)
        .then(pl.min_horizontal(delta_lo, delta_hi))
        .otherwise(None)
        .alias("gp_change_low_cents"),
        pl.when((causal | ivonly) & has_cost & in_support & path_ok)
        .then(pl.max_horizontal(delta_lo, delta_hi))
        .otherwise(None)
        .alias("gp_change_high_cents"),
        pl.when(causal & has_cost & has_k & in_support)
        .then(delta_contrib)
        .otherwise(None)
        .alias("expected_contribution_change_cents"),
        pl.when((causal | ivonly) & has_cost & has_k & in_support & path_ok)
        .then(pl.min_horizontal(delta_contrib_lo, delta_contrib_hi))
        .otherwise(None)
        .alias("contribution_change_low_cents"),
        pl.when((causal | ivonly) & has_cost & has_k & in_support & path_ok)
        .then(pl.max_horizontal(delta_contrib_lo, delta_contrib_hi))
        .otherwise(None)
        .alias("contribution_change_high_cents"),
        pl.when(has_cost.not_())
        .then(pl.lit(TREATMENT_COST_UNAVAILABLE))
        .when((causal | ivonly) & ~path_ok)
        .then(pl.lit(TREATMENT_PATH_UNCONTROLLED))
        .when((causal | ivonly) & ~in_support)
        .then(pl.lit(TREATMENT_OUTSIDE_SUPPORT))
        .when(causal)
        .then(pl.lit(TREATMENT_EXPECTED))
        .when(ivonly)
        .then(pl.lit(TREATMENT_RANGE))
        .otherwise(pl.lit(TREATMENT_FIXED))
        .alias("volume_treatment"),
        pl.when(pl.col("total_quantity_elasticity").is_not_null() & has_cost & in_support)
        .then(delta_total)
        .otherwise(None)
        .alias("gp_change_total_quantity_basis_cents"),
        # Names the estimand behind the modeled volume response, in the words a
        # reader needs: what it holds fixed, and whether the engine has
        # anything to say about what it holds fixed.
        pl.when(pl.col("total_quantity_elasticity").is_not_null())
        .then(
            pl.lit(
                "Modeled on how much buying customers buy, which holds steady the question "
                "of whether they keep buying at all. The second figure re-runs it on the "
                "estimate that counts customers who stop; the two are alternatives, not "
                "parts of a total."
            )
        )
        .otherwise(
            pl.lit(
                "Modeled on how much buying customers buy, which holds steady the question "
                "of whether they keep buying at all. There was not enough evidence to "
                "estimate the second effect here, so it is untested rather than ruled out."
            )
        )
        .alias("volume_estimand_note"),
        pl.lit(f"last_{window}_months").alias("baseline_window"),
        pl.lit("raise_to_target_index").alias("action"),
        pl.lit("target >= floor; corridor P25-P75; contract-protected excluded").alias(
            "constraints"
        ),
        pl.lit("sum of panel qty over baseline window").alias("quantity_basis"),
        pl.format(
            "invoice gap = ({} - {}) x {} {}c; pocket retention {}; qty {}",
            pl.col("target_index").round(4),
            pl.col("current_index").round(4),
            pl.col("price_basis"),
            denom.round(0),
            pl.col("pocket_retention").round(4),
            pl.col("baseline_qty"),
        ).alias("calc_trace"),
    )
    # --- deduction treatment (Run 6, Phase 66) -------------------------------
    tiered_set = sorted({str(c) for c in (tiered_customers or set())})
    if tiered_set:
        g = g.with_columns(
            pl.when(pl.col("customer_id").cast(pl.String).is_in(tiered_set))
            .then(pl.lit(TREATMENT_RECOMPUTED if tiered_ctx is not None else TREATMENT_APPROX))
            .otherwise(pl.lit(TREATMENT_FLAT))
            .alias("deduction_treatment")
        )
    else:
        g = g.with_columns(pl.lit(TREATMENT_FLAT).alias("deduction_treatment"))
    if tiered_ctx is not None and tiered_set:
        g = _recompute_tiered_rows(g, tiered_ctx, tiered_set)

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
        "price_basis",
        "pocket_retention",
        "gross_price_opportunity_cents",
        "gross_price_opportunity_conservative_cents",
        "expected_pocket_gross_profit_change_cents",
        "gp_change_low_cents",
        "gp_change_high_cents",
        "expected_contribution_change_cents",
        "contribution_change_low_cents",
        "contribution_change_high_cents",
        "k_per_unit_cents",
        "k_status",
        "competitor_path_resolution",
        "elasticity_estimand",
        "cost_unavailable",
        "volume_treatment",
        "price_support_distance",
        "price_support_reason",
        "total_quantity_elasticity",
        "total_quantity_status",
        "gp_change_total_quantity_basis_cents",
        "volume_estimand_note",
        "deduction_treatment",
        "baseline_window",
        "action",
        "constraints",
        "quantity_basis",
        "calc_trace",
    )


def _recompute_tiered_rows(
    g: pl.DataFrame, ctx: TieredDeductionContext, tiered_set: list[str]
) -> pl.DataFrame:
    """Replace the flat-rate figures of tiered customers' rows with FULLY
    recomputed ones (Phase 66): the tiered rebate change is evaluated at each
    scenario's candidate revenue - rate(B0 + dR) x (B0 + dR) - rate(B0) x B0 -
    so crossing a tier re-rates the whole basis exactly as a settlement would.
    Rows stay row-identical except for the adjusted dollar columns."""
    is_tiered = pl.col("customer_id").cast(pl.String).is_in(tiered_set)
    rows = g.filter(is_tiered)
    if rows.height == 0:
        return g
    adj_records: list[dict[str, object]] = []
    for r in rows.sort("customer_id", "item_id").to_dicts():
        cust = str(r["customer_id"])
        qty = float(r["baseline_qty"])
        retain = float(r["pocket_retention"])
        p0 = float(r["current_invoice_price_cents"])
        p1 = float(r["proposed_invoice_price_cents"])
        gap = p1 - p0
        rec: dict[str, object] = {"customer_id": r["customer_id"], "item_id": r["item_id"]}
        d2 = ctx.delta_cents(cust, gap * qty)
        if d2 is None:
            continue  # no tiered program after all; flat figures stand
        rec["_adj_gross"] = round(gap * qty * retain - d2)
        cons = r.get("gross_price_opportunity_conservative_cents")
        if cons is not None and retain > 0:
            d2c = ctx.delta_cents(cust, float(cons) / retain) or 0.0
            rec["_adj_cons"] = round(float(cons) - d2c)
        f = float(r.get("fixed_deduction_f") or 0)
        cost = r.get("unit_cost_cents")
        k = r.get("k_per_unit_cents")

        def level3(
            ex: float | None,
            *,
            with_k: bool,
            _cust: str = cust,
            _qty: float = qty,
            _retain: float = retain,
            _p0: float = p0,
            _p1: float = p1,
            _f: float = f,
            _cost: object = cost,
            _k: object = k,
        ) -> int | None:
            if ex is None or _cost is None or (with_k and _k is None):
                return None
            q1 = _qty * (_p1 / _p0) ** float(ex)
            kk = float(_k) if with_k and _k is not None else 0.0  # type: ignore[arg-type]
            c1 = _p1 * _retain - _f - float(_cost) - kk  # type: ignore[arg-type]
            c0 = _p0 * _retain - _f - float(_cost) - kk  # type: ignore[arg-type]
            dr3 = _p1 * q1 - _p0 * _qty
            adj = ctx.delta_cents(_cust, dr3) or 0.0
            value: float = c1 * q1 - c0 * _qty - adj
            return round(value)

        e = r.get("elasticity")
        e_lo, e_hi = r.get("elasticity_ci_low"), r.get("elasticity_ci_high")
        if r.get("expected_pocket_gross_profit_change_cents") is not None:
            rec["_adj_gp"] = level3(e, with_k=False)
        if r.get("expected_contribution_change_cents") is not None:
            rec["_adj_contrib"] = level3(e, with_k=True)
        if r.get("gp_change_low_cents") is not None:
            lo, hi = level3(e_lo, with_k=False), level3(e_hi, with_k=False)
            if lo is not None and hi is not None:
                rec["_adj_gp_lo"], rec["_adj_gp_hi"] = min(lo, hi), max(lo, hi)
        if r.get("contribution_change_low_cents") is not None:
            lo, hi = level3(e_lo, with_k=True), level3(e_hi, with_k=True)
            if lo is not None and hi is not None:
                rec["_adj_c_lo"], rec["_adj_c_hi"] = min(lo, hi), max(lo, hi)
        adj_records.append(rec)
    if not adj_records:
        return g
    adj = pl.DataFrame(adj_records)
    g = g.join(adj, on=["customer_id", "item_id"], how="left", maintain_order="left")
    replacements = {
        "_adj_gross": "gross_price_opportunity_cents",
        "_adj_cons": "gross_price_opportunity_conservative_cents",
        "_adj_gp": "expected_pocket_gross_profit_change_cents",
        "_adj_contrib": "expected_contribution_change_cents",
        "_adj_gp_lo": "gp_change_low_cents",
        "_adj_gp_hi": "gp_change_high_cents",
        "_adj_c_lo": "contribution_change_low_cents",
        "_adj_c_hi": "contribution_change_high_cents",
    }
    exprs = []
    drops = []
    for tmp, target in replacements.items():
        if tmp in g.columns:
            exprs.append(
                pl.when(pl.col(tmp).is_not_null())
                .then(pl.col(tmp))
                .otherwise(pl.col(target))
                .alias(target)
            )
            drops.append(tmp)
    return g.with_columns(exprs).drop(drops)


def numeric_optimal_price(
    *,
    q0: float,
    p0_cents: float,
    beta: float,
    retain: float,
    fixed_f_cents: float,
    unit_cost_cents: float,
    k_marginal_cents: float,
    lo_cents: int,
    hi_cents: int,
) -> int:
    """Numeric argmax of (P x retain - F - C - K_marginal) x Q(P) over
    [lo, hi] (Run 6, Phase 65): a coarse grid then a cent-resolution refine.
    The objective is smooth and unimodal in P for log-log demand, so the
    two-stage search is exact at cent granularity. It agrees with the closed
    form where one exists (tested), and stays correct when unit economics
    depend on quantity through the declared order-cost response."""
    import numpy as np

    if hi_cents <= lo_cents:
        return int(lo_cents)

    def total(p: np.ndarray) -> np.ndarray:
        q = q0 * (p / p0_cents) ** beta
        return (p * retain - fixed_f_cents - unit_cost_cents - k_marginal_cents) * q

    coarse = np.linspace(float(lo_cents), float(hi_cents), 241)
    best = float(coarse[int(np.argmax(total(coarse)))])
    step = (hi_cents - lo_cents) / 240.0
    lo2 = max(lo_cents, int(best - step) - 1)
    hi2 = min(hi_cents, int(best + step) + 1)
    fine = np.arange(lo2, hi2 + 1, dtype=np.float64)
    return int(fine[int(np.argmax(total(fine)))])


def _snap_to_increment(out: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """ERP price precision (Run 6, Phase 67): snap optimized prices to the
    configured increment, never below the binding floor (the floor itself is
    rounded UP to the increment so the snap cannot breach it)."""
    inc = rules.price_guidance.price_increment_cents
    if inc <= 1 or "optimal_price_cents" not in out.columns:
        return out
    price = pl.col("optimal_price_cents")
    snapped = ((price + inc // 2) // inc) * inc
    floor_up = (
        ((pl.col("invoice_floor_cents") + inc - 1) // inc) * inc
        if "invoice_floor_cents" in out.columns
        else snapped
    )
    return out.with_columns(
        pl.when(price.is_not_null())
        .then(pl.max_horizontal(snapped, floor_up))
        .otherwise(None)
        .cast(pl.Int64)
        .alias("optimal_price_cents")
    )


def optimize_targets(
    opportunities: pl.DataFrame, guidance: pl.DataFrame, rules: Rules
) -> pl.DataFrame:
    """P* = argmax of the CONFIGURED objective (Run 6, Phase 65) for log-log
    demand, causal-supported only (24.1). Every row names its objective.

    pocket_gross_profit: (P(1-d) - F - C) x Q(P); the first-order condition
    gives P* = (C + F) / (1 - d) x beta / (1 + beta) for beta < -1.
    contribution: also subtracts the pair's per-unit cost-to-serve. The
    MARGINAL K follows the declared order-cost response: under
    fixed_order_frequency the per-order component's TOTAL is volume-constant
    and drops out of marginal economics; under fixed_order_size it is marginal
    per unit. The contribution optimum is found NUMERICALLY (grid + refine) so
    the path stays correct under quantity-dependent unit economics; a pair
    without a supported K keeps the provisional target - never a borrowed
    number.

    Clipped to [floor, band P75]; for beta >= -1 the unconstrained optimum is
    unbounded, so the corridor top is used and labeled. Pairs without cost
    (24.2) or outside identifying price support (24.6) keep the provisional
    historical target. Everything else keeps provisional too.
    """
    objective = rules.opportunity.optimizer_objective
    g_cols: list[str | pl.Expr] = [
        "customer_id",
        "item_id",
        pl.col("unit_cost_cents"),
        pl.col("invoice_floor_cents"),
        pl.col("eff_list_cents"),
        pl.col("band_p75"),
    ]
    if "deduction_bp" in guidance.columns:
        g_cols += [pl.col("deduction_bp"), pl.col("fixed_deduction_cents")]
    for k_col in ("cts_per_unit_cents", "k_variable_per_unit_cents"):
        if k_col in guidance.columns:
            g_cols.append(pl.col(k_col).alias(f"_opt_{k_col}"))
    g = guidance.select(g_cols)
    j = opportunities.join(g, on=["customer_id", "item_id"], how="left")
    if "deduction_bp" not in j.columns:
        j = j.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("deduction_bp"),
            pl.lit(0, dtype=pl.Int64).alias("fixed_deduction_cents"),
        )
    causal = pl.col("evidence_status") == STATUS_CAUSAL
    # An ALLOW-LIST, not a deny-list (Run 6, Phase 72). The guard used to read
    # `!= TREATMENT_OUTSIDE_SUPPORT`, so every treatment value except that one
    # literal passed - including any value added later and not reasoned about
    # here. Optimizing a price needs a volume response that answers the question
    # being asked, so only the treatments explicitly cleared for it qualify and
    # anything new defaults to no optimization.
    in_support = (
        pl.col("volume_treatment").is_in([TREATMENT_EXPECTED])
        if "volume_treatment" in j.columns
        else pl.lit(value=True)
    )
    if "elasticity" not in j.columns:
        return j.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("optimal_price_cents"),
            pl.lit("provisional_historical").alias("target_basis"),
            pl.lit(objective).alias("objective"),
        )
    beta = pl.col("elasticity")
    # round(6): lossless (deduction_bp is an integer) and kernel-stable; see
    # the same round in price_opportunities.
    retain = ((10_000 - pl.col("deduction_bp").fill_null(0)) / 10_000).round(6)
    # Pocket-aware marginal cost of an invoice dollar: (C + F) / (1 - d).
    c_eff = (pl.col("unit_cost_cents") + pl.col("fixed_deduction_cents").fill_null(0)) / retain
    corridor_hi = (pl.col("band_p75") * pl.col("eff_list_cents")).round(0)

    if objective == "contribution":
        k_total = (
            pl.col("_opt_cts_per_unit_cents").cast(pl.Float64)
            if "_opt_cts_per_unit_cents" in j.columns
            else pl.lit(None, dtype=pl.Float64)
        )
        k_var = (
            pl.col("_opt_k_variable_per_unit_cents").cast(pl.Float64)
            if "_opt_k_variable_per_unit_cents" in j.columns
            else pl.lit(None, dtype=pl.Float64)
        )
        if rules.opportunity.order_cost_response == "fixed_order_size":
            k_marginal = k_total
        else:
            # Fixed ordering cadence: per-order cost is volume-constant in
            # total, so only the variable component is marginal. Without the
            # component split the total K is the conservative stand-in.
            k_marginal = pl.coalesce(k_var, k_total)
        j = j.with_columns(
            k_marginal.alias("_k_marginal"),
            retain.alias("_retain"),
            corridor_hi.alias("_hi"),
        )
        eligible = (
            causal
            & in_support
            & pl.col("unit_cost_cents").is_not_null()
            & pl.col("_k_marginal").is_not_null()
        )
        j = j.with_columns(
            (eligible & (beta < -1.0)).alias("_interior"), eligible.alias("_eligible")
        )
        prices: list[int | None] = []
        for r in j.iter_rows(named=True):
            if not r["_interior"]:
                prices.append(None)
                continue
            lo = int(r["invoice_floor_cents"]) if r["invoice_floor_cents"] is not None else 1
            hi = int(r["_hi"]) if r["_hi"] is not None else lo
            prices.append(
                numeric_optimal_price(
                    q0=float(r["baseline_qty"]),
                    p0_cents=float(r["current_invoice_price_cents"]),
                    beta=float(r["elasticity"]),
                    retain=float(r["_retain"]),
                    fixed_f_cents=float(r["fixed_deduction_cents"] or 0),
                    unit_cost_cents=float(r["unit_cost_cents"]),
                    k_marginal_cents=float(r["_k_marginal"]),
                    lo_cents=lo,
                    hi_cents=max(hi, lo),
                )
            )
        j = j.with_columns(pl.Series("_p_num", prices, dtype=pl.Int64))
        out = j.with_columns(
            pl.when(pl.col("_interior"))
            .then(pl.col("_p_num"))
            .when(pl.col("_eligible"))
            .then(corridor_hi)  # inelastic: corridor top, labeled below
            .otherwise(None)
            .cast(pl.Int64)
            .alias("optimal_price_cents"),
            pl.when(pl.col("_interior"))
            .then(pl.lit("optimized_numeric_contribution_clipped"))
            .when(pl.col("_eligible"))
            .then(pl.lit("corridor_top_inelastic"))
            .otherwise(pl.lit("provisional_historical"))
            .alias("target_basis"),
            pl.lit(objective).alias("objective"),
        ).drop("_k_marginal", "_retain", "_hi", "_interior", "_eligible", "_p_num")
        return _snap_to_increment(out, rules)

    interior = causal & in_support & (beta < -1.0) & pl.col("unit_cost_cents").is_not_null()
    p_star = (c_eff * beta / (1.0 + beta)).round(0)
    clipped = pl.min_horizontal(
        pl.max_horizontal(p_star, pl.col("invoice_floor_cents")), corridor_hi
    )
    out = j.with_columns(
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
        pl.lit(objective).alias("objective"),
    )
    return _snap_to_increment(out, rules)
