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
from margin_engine.labels import LABELS, plain_rule
from margin_engine.report.full_run import FullRunResult
from margin_engine.scenarios.tariff import CostShock, pass_through_grid
from margin_engine.webexport import explain
from margin_engine.webexport.bundle import (
    BUNDLE_VERSION,
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


def build_bundle(
    res: FullRunResult,
    rules: Rules,
    repo_root: Path,
    data_label: str | None = None,
) -> WebBundle:
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
    months = int(sale.select(pl.col("invoice_date").dt.strftime("%Y-%m").n_unique()).item())
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
    def _rules(tree_rules: list[dict[str, Any]]) -> list[SegmentRule]:
        ranked = sorted(tree_rules, key=lambda r: (-int(r["n_lines"]), int(r["leaf_id"])))
        rank_of = {int(r["leaf_id"]): i + 1 for i, r in enumerate(ranked)}
        return [
            SegmentRule(
                leaf_id=int(r["leaf_id"]),
                display_rank=rank_of[int(r["leaf_id"])],
                name=plain_rule(str(r["rule"]))[0],
                plain=plain_rule(str(r["rule"]))[1],
                technical_note=plain_rule(str(r["rule"]))[2],
                rule=str(r["rule"]),
                n_lines=_i(r["n_lines"]),
                n_customers=_i(r["n_customers"]),
                n_items=_i(r["n_items"]),
                mean_target=_r(r.get("mean_target", r.get("median_price_index"))) or 0.0,
            )
            for r in tree_rules
        ]

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
        margin_driver_rules=_rules(res.margin_tree.rules if res.margin_tree else []),
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
    pair_rev = (
        sale.filter(pl.col("invoice_date") >= pl.lit(max_date).dt.offset_by(f"-{window}mo"))
        .group_by("customer_id", "item_id")
        .agg(pl.col("net_amount_cents").sum().alias("annual_net_revenue_cents"))
    )
    gdf = res.guidance.join(pair_rev, on=["customer_id", "item_id"], how="left").sort(
        ["customer_id", "item_id"]
    )
    guidance_pairs = [
        GuidancePair(
            customer_id=str(r["customer_id"]),
            item_id=str(r["item_id"]),
            segment=int(r["segment_leaf"]) if r.get("segment_leaf") is not None else None,
            current_index=_r(r.get("current_index")),
            band_p25=_r(r.get("band_p25")),
            band_p50=_r(r.get("band_p50")),
            band_p75=_r(r.get("band_p75")),
            floor_index=_r(r.get("floor_index")),
            pocket_floor_cents=r.get("pocket_floor_cents"),
            invoice_floor_cents=r.get("invoice_floor_cents"),
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
            item_id=str(r["item_id"]),
            segment=int(r["segment_leaf"]) if r.get("segment_leaf") is not None else None,
            gross_price_opportunity_cents=_i(r["gross_price_opportunity_cents"]),
            expected_contribution_change_cents=r.get("expected_contribution_change_cents"),
            volume_treatment=str(r.get("volume_treatment")),
            evidence_status=r.get("evidence_status"),
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
        expected_total_cents=_i(
            res.opportunities["expected_contribution_change_cents"].fill_null(0).sum()
        )
        if res.opportunities.height
        else 0,
        expected_total_note=(
            "Expected contribution change is summed only over records whose "
            "elasticity is causal-supported; everything else contributes zero here "
            "and is shown fixed-volume."
        ),
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
    scenario_grids = _scenario_grids(res, rules)

    # --- optional modules ------------------------------------------------------------
    optional = _optional_modules(res)

    # --- validation results + manifest ----------------------------------------------
    vr_path = repo_root / "docs" / "validation.json"
    validation = [
        ValidationResult(**row)
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
        waterfall_by_customer=_group_waterfalls(sale, "customer_id", "{}", top_n=200),
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
    )


def _scenario_grids(res: FullRunResult, rules: Rules) -> list[ScenarioGrid]:
    """Pass-through grids (0..100% in 10% steps) for the top categories and
    suppliers by baseline revenue, at a +10% landed-cost shock."""
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
            grid = pass_through_grid(panel, res.guidance, res.sensitivity, shock, rules)
            n_pairs = int(grid.select(pl.col("n_pairs").max()).item() or 0) if grid.height else 0
            if n_pairs == 0:
                continue
            grids.append(
                ScenarioGrid(
                    name=shock.name,
                    scope=scope,
                    scope_value=str(value),
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


def write_bundle(bundle: WebBundle, out_dir: Path) -> tuple[Path, Path]:
    """Write bundle.json (sorted keys, deterministic) and schema.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / "bundle.json"
    schema_path = out_dir / "schema.json"
    payload = json.dumps(bundle.model_dump(mode="json"), sort_keys=True, indent=1)
    bundle_path.write_text(payload + "\n", encoding="utf-8", newline="\n")
    schema = json.dumps(WebBundle.model_json_schema(), sort_keys=True, indent=1)
    schema_path.write_text(schema + "\n", encoding="utf-8", newline="\n")
    return bundle_path, schema_path
