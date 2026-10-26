"""Build the WebBundle from a completed FullRunResult (Phase 18).

Every figure is taken (or summed, cents-exact) from the engine's frames —
nothing is recomputed with different logic. Floats pass through a 10
significant-digit rounding so solver last-ulp noise never reaches the bundle;
money stays integer cents. Output JSON is sorted-keys and newline-terminated,
so the bundle is byte-identical across runs on the same input.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from margin_engine import __version__
from margin_engine.config import Rules
from margin_engine.fiscal import build_calendar
from margin_engine.labels import LABELS, plain_rule, render_text
from margin_engine.report.full_run import FullRunResult
from margin_engine.scenarios.tariff import CostShock, pass_through_grid
from margin_engine.webexport import explain
from margin_engine.webexport.bundle import (
    BUNDLE_VERSION,
    ActionEventRow,
    ActionOutcome,
    ActionRow,
    ActionsSection,
    ActionsSummary,
    CalibrationRow,
    CrossPriceEstimate,
    CustomerEconomics,
    CustomerLeakage,
    DispersionSection,
    EconomicsSection,
    GroupWaterfall,
    GuidancePair,
    InventorySection,
    LeakageComponent,
    LibraryWarning,
    ManifestInfo,
    OpportunityRecord,
    OpportunitySection,
    OptionalModules,
    OrderEconomicsSummary,
    PeerGroupStat,
    ReconciliationCheck,
    Residual,
    ScenarioGrid,
    ScenarioGridPoint,
    SegmentIndexStats,
    SegmentRule,
    SegmentsSection,
    SensitivitySegment,
    SummaryKpis,
    UnallocatedSettlement,
    ValidationResult,
    WaterfallStage,
    WebBundle,
)
from margin_engine.webexport.bundle import (
    LabelEntry as LabelEntryModel,
)

HONEST_LABELS = [
    "Historical benchmarks are not optimal prices.",
    "Descriptive elasticities are not causal.",
    "Allocated amounts are not actuals.",
]

NEVER_SUMMED = (
    "The three levels answer different questions (where dollars went, the gross "
    "gap at unchanged volume, the expected change after volume response) and are "
    "never added together."
)

# Residual explanations shared with the HTML report.
from margin_engine.report.full_run import _RESIDUAL_EXPLANATIONS  # noqa: E402


def _r(x: Any) -> float | None:
    """10-significant-digit float for the bundle (deterministic)."""
    if x is None:
        return None
    f = float(x)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return float(f"{f:.10g}")


def _i(x: Any) -> int:
    return int(x or 0)


_STAGES = [
    ("Gross list value", "gross_list_value_cents", "list"),
    ("Invoice revenue", "invoice_revenue_cents", "all"),
    ("Pocket revenue", "pocket_revenue_cents", "all"),
    ("Pocket gross profit", "pocket_gp_cents", "cost"),
    ("Contribution", "contribution_cents", "cost"),
]


def _waterfall(sale: pl.DataFrame) -> list[WaterfallStage]:
    n = max(sale.height, 1)
    gross_list = _i(sale["gross_list_value_cents"].fill_null(0).sum())
    list_cov = sale.filter(pl.col("gross_list_value_cents").is_not_null()).height / n
    cost_known = sale.filter(pl.col("product_cost_cents").is_not_null())
    cost_cov = cost_known.height / n
    out = []
    for stage, col, basis in _STAGES:
        frame = cost_known if basis == "cost" else sale
        amount = _i(frame[col].fill_null(0).sum())
        coverage = {
            "list": f"{list_cov:.1%} of lines have an effective list",
            "all": "all sale lines",
            "cost": f"{cost_cov:.1%} of lines have cost",
        }[basis]
        out.append(
            WaterfallStage(
                stage=stage,
                amount_cents=amount,
                pct_of_list=_r(amount / gross_list) if gross_list else None,
                coverage=coverage,
            )
        )
    return out


def _group_waterfalls(
    sale: pl.DataFrame, key_col: str, label_fmt: str, top_n: int
) -> list[GroupWaterfall]:
    keys = (
        sale.group_by(key_col)
        .agg(pl.col("net_amount_cents").sum().alias("_rev"))
        .sort(["_rev", key_col], descending=[True, False])
        .head(top_n)[key_col]
        .to_list()
    )
    out = []
    for k in keys:
        if k is None:
            continue
        grp = sale.filter(pl.col(key_col) == k)
        out.append(
            GroupWaterfall(
                key=str(k),
                label=label_fmt.format(k),
                net_revenue_cents=_i(grp["net_amount_cents"].sum()),
                stages=_waterfall(grp),
            )
        )
    return out


_LEAK_COMPONENTS = [
    ("On-invoice discounts", "on_invoice_discount_cents"),
    ("Customer rebates (actual)", "rebate_actual_cents"),
    ("Rebate settlements (allocated)", "rebate_settlement_cents"),
    ("Freight concessions", "freight_concession_cents"),
    ("Price-correction credits", "price_credits_cents"),
]


def _leakage_components(leak: pl.DataFrame) -> list[LeakageComponent]:
    total = _i(leak["total_leakage_cents"].sum())
    return [
        LeakageComponent(
            component=name,
            amount_cents=_i(leak[col].sum()),
            share_of_leakage=_r(_i(leak[col].sum()) / total) if total else None,
        )
        for name, col in _LEAK_COMPONENTS
    ]


def _name_maps(
    res: FullRunResult,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """customer, item, supplier and category display names from the mapped
    reference tables; empty when the export carries IDs only."""
    cust: dict[str, str] = {}
    item: dict[str, str] = {}
    supp: dict[str, str] = {}
    cat: dict[str, str] = {}
    customers = res.pipeline.reference.get("customers")
    if customers is not None and "customer_name" in customers.columns:
        cust = {
            r["customer_id"]: r["customer_name"]
            for r in customers.select("customer_id", "customer_name").to_dicts()
            if r["customer_name"]
        }
    items = res.pipeline.reference.get("items")
    if items is not None:
        if "item_description" in items.columns:
            item = {
                r["item_id"]: r["item_description"]
                for r in items.select("item_id", "item_description").to_dicts()
                if r["item_description"]
            }
        if "supplier_name" in items.columns:
            supp = {
                r["supplier_id"]: r["supplier_name"]
                for r in items.select("supplier_id", "supplier_name").unique().to_dicts()
                if r["supplier_name"]
            }
        if "category_name" in items.columns:
            cat = {
                r["item_category"]: r["category_name"]
                for r in items.select("item_category", "category_name").unique().to_dicts()
                if r["category_name"]
            }
    return cust, item, supp, cat


def build_bundle(
    res: FullRunResult,
    rules: Rules,
    repo_root: Path,
    data_label: str | None = None,
) -> WebBundle:
    cust_name, item_name, supp_name, cat_name = _name_maps(res)
    ledger = res.pipeline.ledger
    sale = ledger.filter(pl.col("transaction_type") == "sale")
    seg_of_line = res.segments.leaf_assignments
    sale_seg = sale.join(seg_of_line, on="source_line_key", how="left")

    # --- KPIs: one basis - everything after returns, so pocket revenue can
    # never exceed net revenue. The waterfall stays sale-lines and says so;
    # returns detail lives in Customers & orders.
    econ = ledger.filter(pl.col("transaction_type").is_in(["sale", "return"]))
    net_rev = _i(sale["net_amount_cents"].sum())
    cost_known = econ.filter(pl.col("product_cost_cents").is_not_null())
    pocket_rev = _i(econ["pocket_revenue_cents"].sum())
    pocket_rev_cost_known = _i(cost_known["pocket_revenue_cents"].sum())
    pocket_gp = _i(cost_known["pocket_gp_cents"].sum())
    returns = ledger.filter(pl.col("transaction_type") == "return")
    returns_cents = _i(returns["net_amount_cents"].sum())
    # Periods covered, in the configured fiscal calendar (Phase 36; the
    # default calendar counts calendar months exactly as before).
    _cal = res.fiscal if res.fiscal is not None else build_calendar(rules.fiscal)
    if _cal.is_calendar_months:
        months = int(sale.select(pl.col("invoice_date").dt.strftime("%Y-%m").n_unique()).item())
    else:
        months = int(
            _cal.attach(sale, "invoice_date", period="_fp").select(pl.col("_fp").n_unique()).item()
        )
    kpis = SummaryKpis(
        n_ledger_lines=ledger.height,
        n_sales_observations=res.pipeline.observations.height,
        n_customers=sale["customer_id"].n_unique(),
        n_items=sale["item_id"].n_unique(),
        months_covered=months,
        window_label=f"{months} months",
        gross_sales_cents=net_rev,
        returns_cents=returns_cents,
        net_revenue_cents=net_rev + returns_cents,
        pocket_revenue_cents=pocket_rev,
        pocket_gross_profit_cents=pocket_gp,
        contribution_cents=_i(cost_known["contribution_cents"].sum()),
        pocket_margin_pct=_r(pocket_gp / pocket_rev_cost_known)
        if pocket_rev_cost_known > 0
        else None,
        reconciliation_pass=res.pipeline.reconciliation.all_pass,
        missing_cost_revenue_share=_r(
            _i(sale.filter(pl.col("flag_missing_cost"))["net_amount_cents"].sum()) / max(net_rev, 1)
        )
        or 0.0,
        list_price_coverage_share=_r(
            sale.filter(pl.col("gross_list_value_cents").is_not_null()).height / max(sale.height, 1)
        )
        or 0.0,
        n_visible_residuals=sum(1 for v in res.pipeline.reconciliation.residuals.values() if v),
        visible_residual_cents=sum(
            abs(v) for v in res.pipeline.reconciliation.residuals.values() if v
        ),
    )

    # --- reconciliation and residuals ---------------------------------------
    recon = [
        ReconciliationCheck(
            check=c["check"],
            expected_cents=_i(c.get("expected_cents")),
            actual_cents=_i(c.get("actual_cents")),
            allocated_cents=c.get("allocated_cents"),
            residual_cents=c.get("residual_cents"),
            source_total_cents=c.get("source_total_cents"),
            passed=bool(c["pass"]),
        )
        for c in res.pipeline.reconciliation.checks
    ]
    fallback = {
        "what": "Residual recorded by the pipeline.",
        "why": "Kept visible rather than forced onto lines.",
        "where": "reconciliation.csv.",
    }
    residuals = [
        Residual(
            name=k,
            amount_cents=int(v),
            what=_RESIDUAL_EXPLANATIONS.get(k, fallback)["what"],
            why_not_allocated=_RESIDUAL_EXPLANATIONS.get(k, fallback)["why"],
            where_listed=_RESIDUAL_EXPLANATIONS.get(k, fallback)["where"],
        )
        for k, v in res.pipeline.reconciliation.residuals.items()
        if v
    ]
    unalloc = [
        UnallocatedSettlement(
            customer_id=r["customer_id"],
            period=r["period"],
            rebate_program_id=r["rebate_program_id"],
            amount_cents=_i(r["amount_cents"]),
        )
        for r in (
            res.unallocated_settlements
            if res.unallocated_settlements is not None
            else pl.DataFrame()
        ).to_dicts()
    ]
    warnings_ = [
        LibraryWarning(
            category=str(w["category"]),
            message=str(w["message"]),
            n_occurrences=int(str(w["n_occurrences"])),
        )
        for w in res.library_warnings
    ]

    # --- dispersion ----------------------------------------------------------
    ps = res.peer_stats if res.peer_stats is not None else pl.DataFrame()
    top_groups = [
        PeerGroupStat(
            peer_group=str(r["peer_group"]),
            n_observations=_i(r["n_lines"]),
            median_index=_r(r["median"]) or 0.0,
            iqr=_r(r["iqr"]) or 0.0,
            p10=_r(r["p10"]) or 0.0,
            p90=_r(r["p90"]) or 0.0,
            mean_discount_to_list=_r(r["mean_discount_to_list"]) or 0.0,
            n_data_quality_flags=_i(r["n_data_quality"]),
            n_commercial_outlier_flags=_i(r["n_commercial_outliers"]),
        )
        for r in ps.sort(["n_lines", "peer_group"], descending=[True, False]).head(50).to_dicts()
    ]
    abc_counts = (
        dict(
            res.flagged.unique(subset=["item_id"])
            .group_by("revenue_abc")
            .len()
            .drop_nulls()
            .iter_rows()
        )
        if "revenue_abc" in res.flagged.columns
        else {}
    )
    dispersion = DispersionSection(
        n_peer_groups=ps.height,
        n_data_quality_flags=_i(res.flagged["flag_data_quality_anomaly"].sum()),
        n_commercial_outlier_flags=_i(res.flagged["flag_commercial_pricing_outlier"].sum()),
        flag_note=(
            "data_quality_anomaly (suspected entry errors) and "
            "commercial_pricing_outlier (unusual but real prices) are separate "
            "flags and are never mixed."
        ),
        top_groups=top_groups,
        abc_revenue_counts={str(k): int(v) for k, v in abc_counts.items()},
    )

    # --- segments ------------------------------------------------------------
    def _driver_meaning(mean: float, overall: float) -> str:
        """One-line 'what this means' for a margin driver, from its numbers."""
        delta_pts = (mean - overall) * 100
        if abs(delta_pts) < 0.05:
            return (
                f"Lines in this group average a {mean * 100:.1f}% pocket margin, "
                f"in line with the overall average of {overall * 100:.1f}%."
            )
        direction = "above" if delta_pts > 0 else "below"
        return (
            f"Lines in this group average a {mean * 100:.1f}% pocket margin, "
            f"{abs(delta_pts):.1f} points {direction} the overall average of "
            f"{overall * 100:.1f}%."
        )

    def _rules(tree_rules: list[dict[str, Any]], *, drivers: bool = False) -> list[SegmentRule]:
        ranked = sorted(tree_rules, key=lambda r: (-int(r["n_lines"]), int(r["leaf_id"])))
        rank_of = {int(r["leaf_id"]): i + 1 for i, r in enumerate(ranked)}
        total_lines = sum(int(r["n_lines"]) for r in tree_rules)
        overall = (
            sum(float(r["mean_target"]) * int(r["n_lines"]) for r in tree_rules) / total_lines
            if drivers and total_lines
            else 0.0
        )
        out = []
        for r in tree_rules:
            pr = plain_rule(str(r["rule"]), frozenset(res.pipeline.mapping.display.acronyms))
            mean = _r(r.get("mean_target", r.get("median_price_index"))) or 0.0
            out.append(
                SegmentRule(
                    leaf_id=int(r["leaf_id"]),
                    display_rank=rank_of[int(r["leaf_id"])],
                    name=pr.name,
                    plain=pr.description,
                    conditions=pr.conditions,
                    technical_note=pr.note,
                    meaning=_driver_meaning(mean, overall) if drivers else None,
                    n_lines=_i(r["n_lines"]),
                    n_customers=_i(r["n_customers"]),
                    n_items=_i(r["n_items"]),
                    mean_target=mean,
                )
            )
        return out

    idx_dist = [
        SegmentIndexStats(
            segment=int(r["segment_leaf"]),
            n_pairs=_i(r["n"]),
            p25=_r(r["p25"]) or 0.0,
            p50=_r(r["p50"]) or 0.0,
            p75=_r(r["p75"]) or 0.0,
        )
        for r in (
            res.guidance.filter(
                pl.col("segment_leaf").is_not_null() & pl.col("current_index").is_not_null()
            )
            .group_by("segment_leaf")
            .agg(
                pl.len().alias("n"),
                pl.col("current_index").quantile(0.25).alias("p25"),
                pl.col("current_index").quantile(0.50).alias("p50"),
                pl.col("current_index").quantile(0.75).alias("p75"),
            )
            .sort("segment_leaf")
            .to_dicts()
        )
    ]
    segments = SegmentsSection(
        rules=_rules(res.segments.rules),
        margin_driver_rules=_rules(res.margin_tree.rules if res.margin_tree else [], drivers=True),
        margin_driver_note=(
            "Margin-driver leaves are explanatory drivers of pocket margin; they "
            "use discount and cost features and are never segments that justify "
            "price increases."
        ),
        bootstrap_median_ari=_r(res.segments.bootstrap_median_ari),
        recommendation_change_share=_r(res.segments.recommendation_change_share),
        index_distribution=idx_dist,
    )

    # --- guidance (all pairs, with explanations and annual revenue) ----------
    max_date = sale["invoice_date"].max()
    window = rules.opportunity.baseline_window_months
    if _cal.is_calendar_months:
        recent = sale.filter(
            pl.col("invoice_date") >= pl.lit(max_date).dt.offset_by(f"-{window}mo")
        )
    else:
        recent = _cal.attach(sale, "invoice_date", idx="_fp_idx")
        max_fp = int(recent["_fp_idx"].max() or 0)  # type: ignore[arg-type]
        recent = recent.filter(pl.col("_fp_idx") > max_fp - window).drop("_fp_idx")
    pair_rev = recent.group_by("customer_id", "item_id").agg(
        pl.col("net_amount_cents").sum().alias("annual_net_revenue_cents")
    )
    gdf = res.guidance.join(pair_rev, on=["customer_id", "item_id"], how="left").sort(
        ["customer_id", "item_id"]
    )
    guidance_pairs = [
        GuidancePair(
            customer_id=str(r["customer_id"]),
            customer_name=cust_name.get(str(r["customer_id"])),
            item_id=str(r["item_id"]),
            item_name=item_name.get(str(r["item_id"])),
            segment=int(r["segment_leaf"]) if r.get("segment_leaf") is not None else None,
            current_index=_r(r.get("current_index")),
            band_p25=_r(r.get("band_p25")),
            band_p50=_r(r.get("band_p50")),
            band_p75=_r(r.get("band_p75")),
            floor_index=_r(r.get("floor_index")),
            pocket_floor_cents=r.get("pocket_floor_cents"),
            invoice_floor_cents=r.get("invoice_floor_cents"),
            invoice_floor_gross_cents=r.get("invoice_floor_gross_cents"),
            invoice_contribution_floor_cents=r.get("invoice_contribution_floor_cents"),
            binding_floor_basis=r.get("binding_floor_basis"),
            list_provenance=r.get("list_provenance"),
            target_index=_r(r.get("target_index")),
            target_label=r.get("target_label"),
            status=str(r.get("guidance_status")),
            annual_net_revenue_cents=r.get("annual_net_revenue_cents"),
            explanation=explain.guidance_explanation(r),
        )
        for r in gdf.to_dicts()
    ]

    # --- sensitivity ----------------------------------------------------------
    sens_rows = []
    for r in res.sensitivity.sort("segment").to_dicts():
        naive, iv = r.get("naive_fe_ols"), r.get("iv")
        if naive is not None and iv is not None:
            note = (
                f"The naive within estimate ({float(naive):.2f}) is closer to zero than "
                f"the IV estimate ({float(iv):.2f}): reps discount when demand is weak, "
                "which biases uncorrected estimates toward zero."
            )
        else:
            note = "Naive and IV estimates are not both available for this segment."
        sens_rows.append(
            SensitivitySegment(
                segment=int(r["segment"]),
                n_obs=_i(r["n_obs"]),
                n_pairs=_i(r["n_pairs"]),
                n_positive=_i(r["n_positive"]),
                price_cv_within=_r(r["price_cv_within"]) or 0.0,
                naive_fe_ols=_r(naive),
                naive_se=_r(r.get("naive_se")),
                ppml=_r(r.get("ppml")),
                iv=_r(iv),
                iv_se=_r(r.get("iv_se")),
                iv_ci_low=_r(r.get("iv_ci_low")),
                iv_ci_high=_r(r.get("iv_ci_high")),
                first_stage_f=_r(r.get("first_stage_f")),
                overid_j=_r(r.get("overid_j")),
                overid_p=_r(r.get("overid_p")),
                ar_ci_low=_r(r.get("ar_ci_low")),
                ar_ci_high=_r(r.get("ar_ci_high")),
                robustness_spread=_r(r.get("robustness_spread")),
                identifying_coverage=_r(r.get("identifying_coverage")),
                ar_bounded=r.get("ar_bounded"),
                support_log_lo=_r(r.get("support_log_lo")),
                support_log_hi=_r(r.get("support_log_hi")),
                ppml_positive=_r(r.get("ppml_positive")),
                carried_forward_share=_r(r.get("carried_forward_share")),
                market_control_used=bool(r.get("market_control_used") or False),
                status=str(r["status"]),
                status_reasons=str(r.get("status_reasons") or ""),
                naive_vs_iv_note=note,
            )
        )

    # --- opportunity -----------------------------------------------------------
    leak = res.leakage
    opp_df = res.opportunities.join(
        res.guidance.select("customer_id", "item_id", "current_index", "target_index"),
        on=["customer_id", "item_id"],
        how="left",
    ).sort(
        ["gross_price_opportunity_cents", "customer_id", "item_id"],
        descending=[True, False, False],
    )
    top_opps = [
        OpportunityRecord(
            customer_id=str(r["customer_id"]),
            customer_name=cust_name.get(str(r["customer_id"])),
            item_id=str(r["item_id"]),
            item_name=item_name.get(str(r["item_id"])),
            segment=int(r["segment_leaf"]) if r.get("segment_leaf") is not None else None,
            gross_price_opportunity_cents=_i(r["gross_price_opportunity_cents"]),
            gross_price_opportunity_conservative_cents=r.get(
                "gross_price_opportunity_conservative_cents"
            ),
            expected_pocket_gross_profit_change_cents=r.get(
                "expected_pocket_gross_profit_change_cents"
            ),
            expected_contribution_change_cents=r.get("expected_contribution_change_cents"),
            volume_treatment=str(r.get("volume_treatment")),
            deduction_treatment=str(r.get("deduction_treatment") or "flat_rate_exact"),
            cost_unavailable=bool(r.get("cost_unavailable") or False),
            evidence_status=r.get("evidence_status"),
            pct_below_target=_r(
                (float(r["target_index"]) - float(r["current_index"])) / float(r["target_index"])
            )
            if r.get("target_index") and r.get("current_index") is not None
            else None,
            summary=explain.opportunity_summary(r),
            explanation=explain.opportunity_explanation(r),
        )
        for r in opp_df.head(200).to_dicts()
    ]
    leak_by_customer = [
        CustomerLeakage(
            customer_id=str(r["customer_id"]),
            net_revenue_cents=_i(r["net_revenue_cents"]),
            total_leakage_cents=_i(r["total_leakage_cents"]),
            components=[
                LeakageComponent(component=name, amount_cents=_i(r[col]))
                for name, col in _LEAK_COMPONENTS
            ],
        )
        for r in leak.to_dicts()
    ]
    opportunity = OpportunitySection(
        never_summed_note=NEVER_SUMMED,
        window_label=f"last {rules.opportunity.baseline_window_months} months",
        leakage_total_cents=_i(leak["total_leakage_cents"].sum()),
        leakage_by_customer=leak_by_customer,
        gross_total_cents=_i(res.opportunities["gross_price_opportunity_cents"].sum())
        if res.opportunities.height
        else 0,
        gross_conservative_total_cents=_i(
            res.opportunities["gross_price_opportunity_conservative_cents"].fill_null(0).sum()
        )
        if res.opportunities.height
        and "gross_price_opportunity_conservative_cents" in res.opportunities.columns
        else 0,
        n_cost_unavailable=_i((res.opportunities["volume_treatment"] == "cost_unavailable").sum())
        if res.opportunities.height
        else 0,
        n_outside_price_support=_i(
            (res.opportunities["volume_treatment"] == "outside_price_support").sum()
        )
        if res.opportunities.height
        else 0,
        expected_gp_total_cents=_i(
            res.opportunities["expected_pocket_gross_profit_change_cents"].fill_null(0).sum()
        )
        if res.opportunities.height
        and "expected_pocket_gross_profit_change_cents" in res.opportunities.columns
        else 0,
        expected_total_cents=_i(
            res.opportunities["expected_contribution_change_cents"].fill_null(0).sum()
        )
        if res.opportunities.height
        else 0,
        expected_total_note=(
            "Two expected-change bases, never conflated: the pocket gross profit "
            "change uses unit economics net of deductions and product cost; the "
            "contribution change also subtracts estimated per-unit cost-to-serve. "
            "Each sums only records whose elasticity is causal-supported; "
            "everything else contributes zero here and is shown fixed-volume."
        ),
        optimizer_objective=rules.opportunity.optimizer_objective,
        top_opportunities=top_opps,
        n_records=res.opportunities.height,
    )

    # --- economics ---------------------------------------------------------------
    flags_by_customer: dict[str, list[str]] = {}
    if res.service_flags is not None:
        for r in res.service_flags.to_dicts():
            names = [
                label
                for col, label in (
                    ("flag_minimum_order_policy", "minimum_order_policy"),
                    ("flag_delivery_frequency_policy", "delivery_frequency_policy"),
                )
                if r.get(col)
            ]
            flags_by_customer[str(r["customer_id"])] = names
    ce = (res.customer_economics if res.customer_economics is not None else pl.DataFrame()).sort(
        "customer_id"
    )
    customers = [
        CustomerEconomics(
            customer_id=str(r["customer_id"]),
            customer_name=cust_name.get(str(r["customer_id"])),
            n_orders=_i(r["n_orders"]),
            net_revenue_cents=_i(r["net_cents"]),
            sales_contribution_cents=_i(r["sales_contribution_cents"]),
            returns_contribution_cents=_i(r["returns_contribution_cents"]),
            contribution_cents=_i(r["contribution_cents"]),
            activity_cts_cents=_i(r["activity_cts_cents"]),
            contribution_after_activity_cents=_i(r["contribution_after_activity_cents"]),
            pct_revenue_excluded=_r(r["pct_revenue_excluded"]) or 0.0,
            avg_order_value_cents=_i(r["avg_order_value_cents"]),
            service_flags=flags_by_customer.get(str(r["customer_id"]), []),
        )
        for r in ce.head(500).to_dicts()
    ]
    oe = res.order_economics if res.order_economics is not None else pl.DataFrame()
    c = rules.cost_to_serve
    orders = OrderEconomicsSummary(
        n_orders=oe.height,
        median_order_value_cents=_i(oe["net_cents"].median()) if oe.height else 0,
        orders_below_min_value=oe.filter(pl.col("net_cents") < c.min_order_value_cents).height
        if oe.height
        else 0,
        min_order_value_cents=c.min_order_value_cents,
        negative_contribution_orders=oe.filter(
            pl.col("contribution_after_activity_cents") < 0
        ).height
        if oe.height
        else 0,
        activity_status_note=(
            "Activity cost-to-serve uses config rates and is labeled estimated; "
            "the base contribution is kept alongside it."
        ),
    )
    economics = EconomicsSection(customers=customers, orders=orders)

    # --- scenario grids ------------------------------------------------------------
    scenario_grids = _scenario_grids(res, rules, supp_name, cat_name)

    # --- optional modules ------------------------------------------------------------
    optional = _optional_modules(res)

    # --- validation results + manifest ----------------------------------------------
    vr_path = repo_root / "docs" / "validation.json"
    # validation.json is written for maintainers and may carry machine codes
    # (preset names, cluster options, status counts); render them through the
    # label dictionary so no raw code reaches the page.
    validation = [
        ValidationResult(
            check=render_text(str(row["check"])),
            target=render_text(str(row["target"])),
            achieved=render_text(str(row["achieved"])),
            passed=bool(row["passed"]),
        )
        for row in json.loads(vr_path.read_text(encoding="utf-8"))["results"]
    ]
    m = res.manifest
    manifest = ManifestInfo(
        rules_sha256=str(m.get("rules_sha256", "")),
        mapping_sha256=str(m.get("mapping_sha256", "")),
        input_files=[
            {
                "name": str(Path(str(f.get("path", f.get("name", "")))))
                .split("\\")[-1]
                .split("/")[-1],
                "sha256": str(f.get("sha256", "")),
            }
            for f in m.get("input_files", [])
        ],
        software_versions={k: str(v) for k, v in m.get("software_versions", {}).items()},
        cleaning_decisions={
            k: int(v) for k, v in m.get("cleaning_decisions", {}).items() if isinstance(v, int)
        },
        notes=[str(n) for n in m.get("parameters", {}).get("notes", [])],
        note_on_volatile_fields=(
            "created_utc and stage timings are excluded from this bundle so it is "
            "byte-identical across runs; they remain in the engine's run manifest."
        ),
    )

    # --- Action center (Phases 38-42) ---------------------------------------
    # The static demo ships the generated actions READ-ONLY with a simulated,
    # lifecycle-valid review history (labeled demo data) and real measured
    # outcomes computed by the Phase 41 tracker against this bundle's ledger.
    from margin_engine.actions.demo import apply_demo_realism, simulate_demo_history
    from margin_engine.actions.model import ACTIONABLE_STATUSES, replay_status
    from margin_engine.actions.tracking import track_outcomes

    act_frame = res.actions if res.actions is not None else pl.DataFrame()
    actions_table: list[ActionRow] = []
    if act_frame.height:
        act_frame = apply_demo_realism(act_frame, rules)
        demo_events = simulate_demo_history(act_frame)
        tracked = track_outcomes(
            act_frame,
            demo_events,
            res.pipeline.ledger,
            rules,
            pair_segments=res.guidance.select("customer_id", "item_id", "segment_leaf"),
        )
        statuses = {r[0]: r[1] for r in replay_status(demo_events).iter_rows()}
        events_by_action: dict[str, list[ActionEventRow]] = {}
        for e in demo_events.sort("action_id", "seq").iter_rows(named=True):
            events_by_action.setdefault(str(e["action_id"]), []).append(
                ActionEventRow(
                    seq=int(e["seq"]),
                    who=str(e["who"]),
                    at=e["at"].isoformat(),
                    from_status=e["from_status"],
                    to_status=str(e["to_status"]),
                    note=str(e["note"] or ""),
                )
            )
        outcomes_by_action: dict[str, ActionOutcome] = {}
        for o in tracked.outcomes.iter_rows(named=True):
            outcomes_by_action[str(o["action_id"])] = ActionOutcome(
                implemented_on=str(o["implemented_on"]),
                window_days=int(o["window_days"]),
                qty_before=o["qty_before"],
                qty_after=o["qty_after"],
                price_before_cents=o["price_before_cents"],
                price_after_cents=o["price_after_cents"],
                proposed_price_cents=o["proposed_price_cents"],
                realization_pct=o["realization_pct"],
                predicted_volume_ratio=o["predicted_volume_ratio"],
                treated_volume_ratio=o["treated_volume_ratio"],
                control_volume_ratio=o["control_volume_ratio"],
                n_control_pairs=int(o["n_control_pairs"] or 0),
                did_volume_ratio=o["did_volume_ratio"],
                volume_error_log_points=o["volume_error_log_points"],
                pocket_margin_before=o["pocket_margin_before"],
                pocket_margin_after=o["pocket_margin_after"],
                contribution_before_cents=o["contribution_before_cents"],
                contribution_after_cents=o["contribution_after_cents"],
                outcome_status=str(o["outcome_status"]),
                outcome_reason=str(o["outcome_reason"] or ""),
            )
        for a in act_frame.sort(["action_type", "action_id"]).iter_rows(named=True):
            aid = str(a["action_id"])
            a_status = statuses.get(aid, "proposed")
            is_overdue = (
                a_status in ACTIONABLE_STATUSES
                and a["due_date"] is not None
                and a["due_date"] < a["created_at"].date()  # created_at = as-of anchor
            )
            actions_table.append(
                ActionRow(
                    action_id=aid,
                    action_type=str(a["action_type"]),
                    title=str(a["title"]),
                    rationale=str(a["rationale"]),
                    customer_id=a["customer_id"],
                    customer_name=cust_name.get(str(a["customer_id"]))
                    if a["customer_id"]
                    else None,
                    item_id=a["item_id"],
                    item_name=item_name.get(str(a["item_id"])) if a["item_id"] else None,
                    scope=str(a["scope"]),
                    uom=a["uom"],
                    segment=a["segment_leaf"],
                    guidance_status=a["guidance_status"],
                    evidence_status=a["evidence_status"],
                    volume_treatment=a["volume_treatment"],
                    predicted_volume_ratio=a["predicted_volume_ratio"],
                    impact_basis=str(a["impact_basis"]),
                    expected_impact_cents=a["expected_impact_cents"],
                    current_price_cents=a["current_price_cents"],
                    proposed_price_cents=a["proposed_price_cents"],
                    floor_price_cents=a["floor_price_cents"],
                    pass_through_level=a["pass_through_level"],
                    incremental_cost_cents=a["incremental_cost_cents"],
                    priority=str(a["priority"]),
                    owner=str(a["owner"]),
                    due_date=str(a["due_date"]) if a["due_date"] is not None else None,
                    overdue=is_overdue,
                    compliance_flags=str(a["compliance_flags"] or ""),
                    blocked=bool(a["blocked"]),
                    blocked_reason=a["blocked_reason"],
                    status=a_status,
                    events=events_by_action.get(aid, []),
                    outcome=outcomes_by_action.get(aid),
                )
            )
        calibration_rows = [
            CalibrationRow(
                bucket=str(c["bucket"]),
                n_measured=int(c["n_measured"]),
                median_abs_volume_error_log_points=c["median_abs_volume_error_log_points"],
                median_realization_pct=c["median_realization_pct"],
                status=str(c["status"]),
            )
            for c in tracked.calibration.iter_rows(named=True)
        ]
        measured_rows = [
            a.outcome for a in actions_table if a.outcome and a.outcome.outcome_status == "measured"
        ]
        realizations = sorted(
            m.realization_pct
            for m in measured_rows
            if m is not None and m.realization_pct is not None
        )
        by_status_counts: dict[str, int] = {}
        impact_by_evidence: dict[str, int] = {}
        impact_by_basis: dict[str, int] = {}
        for arow in actions_table:
            by_status_counts[arow.status] = by_status_counts.get(arow.status, 0) + 1
            ev = arow.evidence_status or "insufficient_data"
            impact_by_evidence[ev] = impact_by_evidence.get(ev, 0) + int(
                arow.expected_impact_cents or 0
            )
            impact_by_basis[arow.impact_basis] = impact_by_basis.get(arow.impact_basis, 0) + int(
                arow.expected_impact_cents or 0
            )
        actions_section = ActionsSection(
            read_only=True,
            demo_history=True,
            summary=ActionsSummary(
                n_actions=len(actions_table),
                by_status=dict(sorted(by_status_counts.items())),
                impact_by_evidence_cents=dict(sorted(impact_by_evidence.items())),
                impact_by_basis_cents=dict(sorted(impact_by_basis.items())),
                n_blocked=sum(1 for a in actions_table if a.blocked),
                n_compliance_flagged=sum(1 for a in actions_table if a.compliance_flags),
                n_measured=len(measured_rows),
                median_realization_pct=(
                    realizations[len(realizations) // 2] if realizations else None
                ),
            ),
            calibration=calibration_rows,
            facets={
                "types": sorted({a.action_type for a in actions_table}),
                "statuses": sorted({a.status for a in actions_table}),
                "owners": sorted({a.owner for a in actions_table}),
                "priorities": sorted({a.priority for a in actions_table}),
                "evidence": sorted(
                    {a.evidence_status or "insufficient_data" for a in actions_table}
                ),
            },
            notes=list(tracked.notes),
        )
    else:
        actions_section = ActionsSection(
            read_only=True,
            demo_history=True,
            summary=ActionsSummary(
                n_actions=0,
                by_status={},
                impact_by_evidence_cents={},
                impact_by_basis_cents={},
                n_blocked=0,
                n_compliance_flagged=0,
                n_measured=0,
            ),
            calibration=[],
            facets={"types": [], "statuses": [], "owners": [], "priorities": [], "evidence": []},
            notes=["action generation produced no actions for this world"],
        )

    return WebBundle(
        bundle_version=BUNDLE_VERSION,
        engine_version=__version__,
        data_label=data_label or "Synthetic demo data (committed sample, planted ground truth)",
        honest_labels=HONEST_LABELS,
        labels={k: LabelEntryModel(**v) for k, v in LABELS.items()},
        kpis=kpis,
        reconciliation=recon,
        residuals=residuals,
        unallocated_settlements=unalloc,
        library_warnings=warnings_,
        waterfall_overall=_waterfall(sale),
        waterfall_by_segment=_group_waterfalls(
            sale_seg.filter(pl.col("segment_leaf").is_not_null()).with_columns(
                pl.col("segment_leaf").cast(pl.Int64)
            ),
            "segment_leaf",
            "Segment {}",
            top_n=50,
        ),
        waterfall_by_customer=[
            gw.model_copy(
                update={
                    "label": f"{cust_name[gw.key]} · {gw.key}" if gw.key in cust_name else gw.label
                }
            )
            for gw in _group_waterfalls(sale, "customer_id", "{}", top_n=200)
        ],
        leakage_components_overall=_leakage_components(leak),
        dispersion=dispersion,
        segments=segments,
        guidance=guidance_pairs,
        sensitivity=sens_rows,
        opportunity=opportunity,
        economics=economics,
        scenario_grids=scenario_grids,
        optional_modules=optional,
        validation_results=validation,
        manifest=manifest,
        actions=actions_section,
        actions_table=actions_table,
    )


def _scenario_grids(
    res: FullRunResult,
    rules: Rules,
    supp_name: dict[str, str],
    cat_name: dict[str, str],
) -> list[ScenarioGrid]:
    """Pass-through grids (0..100% in 1% steps) for the top categories and
    suppliers by baseline revenue, at a +10% landed-cost shock. The fine grid
    lets the UI slider move in 1% steps while every displayed value still
    comes from the engine."""
    from datetime import date

    from margin_engine.extended.inventory import scenario_timing_statement

    if res.panel is None:
        return []
    panel = res.panel
    sale = res.pipeline.observations
    cost_method = (
        res.pipeline.mapping.lines.unit_cost.cost_basis.value
        if res.pipeline.mapping.lines.unit_cost
        else "average"
    )
    timing = scenario_timing_statement(cost_method, rules.tariff_scenarios.cost_pool)
    grids: list[ScenarioGrid] = []
    for scope, col in (("category", "item_category"), ("supplier", "supplier_id")):
        if col not in panel.columns:
            continue
        items_rev = (
            sale.join(
                panel.select("item_id", col).unique(subset=["item_id"]), on="item_id", how="left"
            )
            .filter(pl.col(col).is_not_null())
            .group_by(col)
            .agg(pl.col("net_amount_cents").sum().alias("_rev"))
            .sort(["_rev", col], descending=[True, False])
            .head(4)[col]
            .to_list()
        )
        for value in items_rev:
            shock = CostShock(
                name=f"+10% landed cost: {scope} {value}",
                scope="item" if scope == "item" else scope,
                scope_value=str(value),
                pct_increase=0.10,
                effective=date(2000, 1, 1),  # scenarios apply to the baseline window
            )
            grid = pass_through_grid(panel, res.guidance, res.sensitivity, shock, rules, steps=100)
            n_pairs = int(grid.select(pl.col("n_pairs").max()).item() or 0) if grid.height else 0
            if n_pairs == 0:
                continue
            scope_label = (supp_name if scope == "supplier" else cat_name).get(str(value))
            grids.append(
                ScenarioGrid(
                    name=shock.name,
                    scope=scope,
                    scope_value=str(value),
                    scope_label=scope_label,
                    pct_increase=0.10,
                    n_pairs=n_pairs,
                    cogs_timing=timing,
                    points=[
                        ScenarioGridPoint(
                            pass_through=_r(p["pass_through"]) or 0.0,
                            incremental_cost_cents=_i(p["incremental_cost_cents"]),
                            recovered_cents=_i(p["recovered_cents"]),
                            residual_exposure_cents=_i(p["residual_exposure_cents"]),
                            protected_exposure_cents=_i(p["protected_exposure_cents"]),
                            contribution_effect_cents=int(p["contribution_effect_cents"])
                            if p["contribution_effect_cents"] is not None
                            else None,
                            contribution_effect_supported_everywhere=bool(
                                p["contribution_effect_supported_everywhere"]
                            ),
                        )
                        for p in grid.sort("pass_through").to_dicts()
                    ],
                )
            )
    return grids


def _optional_modules(res: FullRunResult) -> OptionalModules:
    win = None
    win_reason = None
    if res.win_curves is not None:
        win = [
            WinCurveModel(
                segment=int(r["segment"]),
                n_quotes=_i(r["n_quotes"]),
                win_rate=_r(r["win_rate"]) or 0.0,
                price_coef=_r(r.get("price_coef")),
                price_coef_se=_r(r.get("price_coef_se")),
                ci_low=_r(r.get("ci_low")),
                ci_high=_r(r.get("ci_high")),
                negotiation_gap=_r(r.get("negotiation_gap")),
                status=str(r["status"]),
                reasons=str(r.get("reasons") or ""),
            )
            for r in res.win_curves.sort("segment").to_dicts()
        ]
    else:
        win_reason = "No quotes table (or no price list) in the mapped data."
    cross = None
    cross_reason = None
    if res.cross_estimates is not None:
        cross = [
            CrossPriceEstimate(
                group_id=str(r["group_id"]),
                item_a=str(r["item_a"]),
                item_b=str(r["item_b"]),
                n_obs=_i(r["n_obs"]),
                cross_elasticity=_r(r.get("cross_elasticity")),
                cross_se=_r(r.get("cross_se")),
                status=str(r["status"]),
                reasons=str(r.get("reasons") or ""),
            )
            for r in res.cross_estimates.sort(["group_id", "item_a", "item_b"]).to_dicts()
        ]
    else:
        cross_reason = "No substitution-groups reference table in the mapped data."
    inventory = None
    inv_reason = None
    if res.inventory_cogs is not None:
        from margin_engine.extended.inventory import scenario_timing_statement

        method = (
            res.pipeline.mapping.lines.unit_cost.cost_basis.value
            if res.pipeline.mapping.lines.unit_cost
            else "average"
        )
        inventory = InventorySection(
            method=method,
            n_sales_costed=res.inventory_cogs.height,
            n_flagged=res.inventory_cogs.filter(pl.col("cogs_flag") != "").height,
            cogs_total_cents=_i(res.inventory_cogs["cogs_cents"].sum()),
            timing_statement=scenario_timing_statement(method, "existing_pool"),
        )
    else:
        inv_reason = "No inventory-receipts reference table in the mapped data."
    return OptionalModules(
        win_curves=win,
        win_curves_absent_reason=win_reason,
        cross_price=cross,
        cross_price_absent_reason=cross_reason,
        inventory=inventory,
        inventory_absent_reason=inv_reason,
    )


# Imported late to keep the model namespace tidy at the top of the module.
from margin_engine.webexport.bundle import WinCurve as WinCurveModel  # noqa: E402


def _page_payloads(bundle: WebBundle) -> dict[str, dict[str, object]]:
    """Per-page projections of the bundle (the static files behind the data
    client's page endpoints) plus per-table files. Pure projections - every
    figure still comes from the one WebBundle."""
    d = bundle.model_dump(mode="json")
    seg_rules = d["segments"]["rules"]
    guidance = d["guidance"]
    opp = d["opportunity"]
    econ = d["economics"]
    sens = d["sensitivity"]

    def facet_entities(
        rows: list[dict[str, object]], id_key: str, name_key: str
    ) -> list[dict[str, object]]:
        seen: dict[str, object] = {}
        for r in rows:
            rid = str(r[id_key])
            if rid not in seen:
                seen[rid] = r.get(name_key)
        return [{"id": k, "name": v} for k, v in seen.items()]

    seg_names = [
        {"leaf_id": r["leaf_id"], "display_rank": r["display_rank"], "name": r["name"]}
        for r in seg_rules
    ]
    leak_sorted = sorted(d["leakage_components_overall"], key=lambda c: -int(c["amount_cents"]))
    pages: dict[str, dict[str, object]] = {
        "app": {
            "bundle_version": d["bundle_version"],
            "engine_version": d["engine_version"],
            "data_label": d["data_label"],
            "honest_labels": d["honest_labels"],
            "labels": d["labels"],
            "kpis": d["kpis"],
        },
        "overview": {
            "waterfall_overall": d["waterfall_overall"],
            "leakage_total_cents": opp["leakage_total_cents"],
            "leakage_top_component": leak_sorted[0] if leak_sorted else None,
            "gross_total_cents": opp["gross_total_cents"],
            "window_label": opp["window_label"],
            "below_floor_pairs": sum(1 for g in guidance if g["status"] == "below_floor"),
            "causal_segments": sum(1 for r in sens if r["status"] == "causal_elasticity_supported"),
            "total_segments": len(sens),
            "negative_contribution_orders": econ["orders"]["negative_contribution_orders"],
            "n_library_warnings": len(d["library_warnings"]),
            # Open-actions summary (Phase 42): counts by status, expected
            # impact by evidence level, latest measured results.
            "open_actions": d["actions"]["summary"],
        },
        "waterfall": {
            "overall": d["waterfall_overall"],
            "by_segment": d["waterfall_by_segment"],
            "by_customer": d["waterfall_by_customer"],
            "leakage_components": d["leakage_components_overall"],
            "leakage_total_cents": opp["leakage_total_cents"],
            "window_label": opp["window_label"],
            "segment_names": seg_names,
        },
        "customers": {
            "orders": econ["orders"],
            "facets": {
                "customers": facet_entities(econ["customers"], "customer_id", "customer_name"),
                "service_flags": sorted({f for c in econ["customers"] for f in c["service_flags"]}),
            },
            "waterfall_by_customer": d["waterfall_by_customer"],
        },
        "segments": d["segments"],
        "guidance": {
            "n_pairs": len(guidance),
            "facets": {
                "customers": facet_entities(guidance, "customer_id", "customer_name"),
                "items": facet_entities(guidance, "item_id", "item_name"),
                "statuses": sorted({g["status"] for g in guidance}),
                "segments": seg_names,
            },
        },
        "sensitivity": {"rows": sens, "segment_names": seg_names},
        "opportunity": {
            "never_summed_note": opp["never_summed_note"],
            "window_label": opp["window_label"],
            "leakage_total_cents": opp["leakage_total_cents"],
            "gross_total_cents": opp["gross_total_cents"],
            "gross_conservative_total_cents": opp["gross_conservative_total_cents"],
            "expected_gp_total_cents": opp["expected_gp_total_cents"],
            "expected_total_cents": opp["expected_total_cents"],
            "expected_total_note": opp["expected_total_note"],
            "optimizer_objective": opp["optimizer_objective"],
            "n_records": opp["n_records"],
            "n_cost_unavailable": opp["n_cost_unavailable"],
            "n_outside_price_support": opp["n_outside_price_support"],
            "leakage_by_customer": opp["leakage_by_customer"],
            "facets": {
                "customers": facet_entities(
                    opp["top_opportunities"], "customer_id", "customer_name"
                ),
                "items": facet_entities(opp["top_opportunities"], "item_id", "item_name"),
                "evidence": sorted(
                    {
                        str(r["evidence_status"] or "insufficient_data")
                        for r in opp["top_opportunities"]
                    }
                ),
                "treatments": sorted({r["volume_treatment"] for r in opp["top_opportunities"]}),
            },
        },
        "scenarios": {"grids": d["scenario_grids"]},
        "methodology": {
            "data_label": d["data_label"],
            "reconciliation": d["reconciliation"],
            "residuals": d["residuals"],
            "unallocated_settlements": d["unallocated_settlements"],
            "library_warnings": d["library_warnings"],
            "validation_results": d["validation_results"],
            "manifest": d["manifest"],
        },
        "modules": d["optional_modules"],
        # Explanations ride a companion file fetched only when a pair is
        # opened, and null fields are omitted from table rows (absent ==
        # null to the UI) - keeps the table file itself under the size gate.
        "tables/guidance": {
            "items": [
                {k: v for k, v in g.items() if k != "explanation" and v is not None}
                for g in guidance
            ]
        },
        "tables/guidance_explanations": {
            "items": {f"{g['customer_id']}|{g['item_id']}": g["explanation"] for g in guidance}
        },
        "tables/opportunities": {"items": opp["top_opportunities"]},
        "tables/customers": {"items": econ["customers"]},
        "actions": d["actions"],
        "tables/actions": {"items": d["actions_table"]},
    }
    return pages


# Hard per-file limit for app-consumed files is 1 MB; shards aim well below it
# because the size estimate uses compact per-row dumps while files are written
# indented (~10% overhead), and growth headroom should remain. Transport detail
# only: the data client follows the "next" links, and the HTTP adapter never
# sees shards (the BFF paginates).
_SHARD_BYTES = 700_000


def _shard_table(name: str, data: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    """Split a table payload into <= _SHARD_BYTES files chained by "next".
    Lists shard by element; dicts shard by (already deterministic) key order.
    Returns [(file name, payload), ...] - a single unchained file when small."""
    items = data["items"]
    blob = json.dumps(data, sort_keys=True, indent=1)
    if len(blob.encode("utf-8")) <= _SHARD_BYTES:
        return [(name, data)]
    entries: list[tuple[object, int]] = []
    if isinstance(items, dict):
        for k in items:
            entries.append(((k, items[k]), len(json.dumps({k: items[k]}).encode("utf-8"))))
    else:
        assert isinstance(items, list)  # table payloads carry list or dict items
        for it in items:
            entries.append((it, len(json.dumps(it).encode("utf-8"))))
    chunks: list[list[object]] = [[]]
    size = 0
    for entry, nbytes in entries:
        if size + nbytes > _SHARD_BYTES and chunks[-1]:
            chunks.append([])
            size = 0
        chunks[-1].append(entry)
        size += nbytes
    out: list[tuple[str, dict[str, object]]] = []
    for i, chunk in enumerate(chunks):
        fname = name if i == 0 else f"{name}_{i + 1}"
        nxt = f"{name}_{i + 2}.json" if i + 1 < len(chunks) else None
        chunk_items: object = dict(chunk) if isinstance(items, dict) else chunk  # type: ignore[arg-type]
        out.append((fname, {**data, "items": chunk_items, "next": nxt}))
    return out


def write_bundle(bundle: WebBundle, out_dir: Path) -> tuple[Path, Path]:
    """Write bundle.json (full model, for tooling and parity tests),
    schema.json, and the split per-page / per-table files the app consumes -
    all deterministic (sorted keys, newline-terminated). Oversized table files
    are sharded into "next"-linked chunks so every app file stays under 1 MB."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / "bundle.json"
    schema_path = out_dir / "schema.json"
    payload = json.dumps(bundle.model_dump(mode="json"), sort_keys=True, indent=1)
    bundle_path.write_text(payload + "\n", encoding="utf-8", newline="\n")
    schema = json.dumps(WebBundle.model_json_schema(), sort_keys=True, indent=1)
    schema_path.write_text(schema + "\n", encoding="utf-8", newline="\n")
    # Clear old table shards so a shrinking export leaves no orphan files.
    tables_dir = out_dir / "tables"
    if tables_dir.is_dir():
        for old in tables_dir.glob("*.json"):
            old.unlink()
    files: list[tuple[str, dict[str, object]]] = []
    for name, data in _page_payloads(bundle).items():
        if name.startswith("tables/"):
            files.extend(_shard_table(name, data))
        else:
            files.append((name, data))
    for name, data in files:
        path = out_dir / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, sort_keys=True, indent=1) + "\n", encoding="utf-8", newline="\n"
        )
    return bundle_path, schema_path
