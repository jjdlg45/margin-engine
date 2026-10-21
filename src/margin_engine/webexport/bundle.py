"""Bundle schema (pydantic models) for the hosted demo.

Single source of truth: ``bundle.json`` is the serialized ``WebBundle``,
``schema.json`` is ``WebBundle.model_json_schema()``, and the app's TypeScript
types are generated from that schema. Money is integer cents throughout; the
UI formats. Volatile run metadata (timestamps, stage timings) is excluded so
the bundle is byte-identical across runs on the same input.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

BUNDLE_VERSION = "1.0.0"


class SummaryKpis(BaseModel):
    n_ledger_lines: int
    n_sales_observations: int
    n_customers: int
    n_items: int
    months_covered: int
    window_label: str = Field(description="Time window every KPI covers, e.g. '24 months'")
    gross_sales_cents: int = Field(description="Sale lines only, before returns")
    returns_cents: int = Field(description="Linked return lines (negative)")
    net_revenue_cents: int = Field(description="Gross sales plus returns - matches economics")
    pocket_revenue_cents: int = Field(
        description="After returns, like every headline KPI - never exceeds net revenue"
    )
    pocket_gross_profit_cents: int = Field(description="After returns, cost-known lines")
    contribution_cents: int = Field(description="After returns, cost-known lines")
    pocket_margin_pct: float | None = Field(
        default=None, description="Pocket GP / pocket revenue on cost-known lines"
    )
    reconciliation_pass: bool
    missing_cost_revenue_share: float
    list_price_coverage_share: float


class ReconciliationCheck(BaseModel):
    check: str
    expected_cents: int
    actual_cents: int
    allocated_cents: int | None = None
    residual_cents: int | None = None
    source_total_cents: int | None = None
    passed: bool


class Residual(BaseModel):
    name: str
    amount_cents: int
    what: str
    why_not_allocated: str
    where_listed: str


class UnallocatedSettlement(BaseModel):
    customer_id: str
    period: str
    rebate_program_id: str
    amount_cents: int


class LibraryWarning(BaseModel):
    category: str
    message: str
    n_occurrences: int


class WaterfallStage(BaseModel):
    stage: str
    amount_cents: int
    pct_of_list: float | None = Field(default=None, description="None when no list coverage")
    coverage: str


class GroupWaterfall(BaseModel):
    key: str = Field(description="Segment id or customer id")
    label: str
    net_revenue_cents: int
    stages: list[WaterfallStage]


class LeakageComponent(BaseModel):
    component: str
    amount_cents: int
    share_of_leakage: float | None = None


class PeerGroupStat(BaseModel):
    peer_group: str
    n_observations: int
    median_index: float
    iqr: float
    p10: float
    p90: float
    mean_discount_to_list: float
    n_data_quality_flags: int
    n_commercial_outlier_flags: int


class DispersionSection(BaseModel):
    n_peer_groups: int
    n_data_quality_flags: int
    n_commercial_outlier_flags: int
    flag_note: str
    top_groups: list[PeerGroupStat]
    abc_revenue_counts: dict[str, int]


class SegmentRule(BaseModel):
    leaf_id: int = Field(description="Tree node id - technical detail only")
    display_rank: int = Field(description="1-based rank by size; viewers see 'Segment {rank}'")
    name: str = Field(description="Short plain-English name derived from the rule")
    plain: str = Field(description="One-sentence description derived from the rule")
    technical_note: str = Field(description="Merge/leaf mechanics for the expander")
    rule: str = Field(description="Raw tree rule - technical detail, not primary display")
    n_lines: int
    n_customers: int
    n_items: int
    mean_target: float


class SegmentIndexStats(BaseModel):
    segment: int
    n_pairs: int
    p25: float
    p50: float
    p75: float


class SegmentsSection(BaseModel):
    rules: list[SegmentRule]
    margin_driver_rules: list[SegmentRule]
    margin_driver_note: str
    bootstrap_median_ari: float | None
    recommendation_change_share: float | None
    index_distribution: list[SegmentIndexStats]


class GuidancePair(BaseModel):
    customer_id: str
    customer_name: str | None = None
    item_id: str
    item_name: str | None = None
    segment: int | None
    current_index: float | None
    band_p25: float | None
    band_p50: float | None
    band_p75: float | None
    floor_index: float | None
    pocket_floor_cents: int | None
    invoice_floor_cents: int | None
    target_index: float | None
    target_label: str | None
    status: str
    annual_net_revenue_cents: int | None = None
    explanation: str


class SensitivitySegment(BaseModel):
    segment: int
    n_obs: int
    n_pairs: int
    n_positive: int
    price_cv_within: float
    naive_fe_ols: float | None
    naive_se: float | None
    ppml: float | None
    iv: float | None
    iv_se: float | None
    iv_ci_low: float | None
    iv_ci_high: float | None
    first_stage_f: float | None
    overid_j: float | None
    overid_p: float | None
    ar_ci_low: float | None
    ar_ci_high: float | None
    robustness_spread: float | None
    identifying_coverage: float | None
    status: str
    status_reasons: str
    naive_vs_iv_note: str


class OpportunityRecord(BaseModel):
    customer_id: str
    customer_name: str | None = None
    item_id: str
    item_name: str | None = None
    segment: int | None
    gross_price_opportunity_cents: int
    expected_contribution_change_cents: int | None
    volume_treatment: str
    evidence_status: str | None
    pct_below_target: float | None = Field(
        default=None, description="(target - current) / target, from the pair's guidance"
    )
    summary: str = Field(description="One short row-specific sentence for the table")
    explanation: str


class CustomerLeakage(BaseModel):
    customer_id: str
    net_revenue_cents: int
    total_leakage_cents: int
    components: list[LeakageComponent]


class OpportunitySection(BaseModel):
    never_summed_note: str
    window_label: str = Field(description="Baseline window for every figure in this section")
    leakage_total_cents: int
    leakage_by_customer: list[CustomerLeakage]
    gross_total_cents: int
    expected_total_cents: int
    expected_total_note: str
    top_opportunities: list[OpportunityRecord]
    n_records: int


class CustomerEconomics(BaseModel):
    customer_id: str
    customer_name: str | None = None
    n_orders: int
    net_revenue_cents: int
    sales_contribution_cents: int
    returns_contribution_cents: int
    contribution_cents: int
    activity_cts_cents: int
    contribution_after_activity_cents: int
    pct_revenue_excluded: float
    avg_order_value_cents: int
    service_flags: list[str]


class OrderEconomicsSummary(BaseModel):
    n_orders: int
    median_order_value_cents: int
    orders_below_min_value: int
    min_order_value_cents: int
    negative_contribution_orders: int
    activity_status_note: str


class EconomicsSection(BaseModel):
    customers: list[CustomerEconomics]
    orders: OrderEconomicsSummary


class ScenarioGridPoint(BaseModel):
    pass_through: float
    incremental_cost_cents: int
    recovered_cents: int
    residual_exposure_cents: int
    protected_exposure_cents: int
    contribution_effect_cents: int | None
    contribution_effect_supported_everywhere: bool


class ScenarioGrid(BaseModel):
    name: str
    scope: str
    scope_value: str
    scope_label: str | None = Field(default=None, description="Supplier/category display name")
    pct_increase: float
    n_pairs: int
    cogs_timing: str
    points: list[ScenarioGridPoint]


class WinCurve(BaseModel):
    segment: int
    n_quotes: int
    win_rate: float
    price_coef: float | None
    price_coef_se: float | None
    ci_low: float | None
    ci_high: float | None
    negotiation_gap: float | None
    status: str
    reasons: str


class CrossPriceEstimate(BaseModel):
    group_id: str
    item_a: str
    item_b: str
    n_obs: int
    cross_elasticity: float | None
    cross_se: float | None
    status: str
    reasons: str


class InventorySection(BaseModel):
    method: str
    n_sales_costed: int
    n_flagged: int
    cogs_total_cents: int
    timing_statement: str


class OptionalModules(BaseModel):
    win_curves: list[WinCurve] | None
    win_curves_absent_reason: str | None
    cross_price: list[CrossPriceEstimate] | None
    cross_price_absent_reason: str | None
    inventory: InventorySection | None
    inventory_absent_reason: str | None


class ValidationResult(BaseModel):
    check: str
    target: str
    achieved: str
    passed: bool


class ManifestInfo(BaseModel):
    rules_sha256: str
    mapping_sha256: str
    input_files: list[dict[str, str]]
    software_versions: dict[str, str]
    cleaning_decisions: dict[str, int]
    notes: list[str]
    note_on_volatile_fields: str


class LabelEntry(BaseModel):
    label: str
    description: str


class WebBundle(BaseModel):
    bundle_version: str
    engine_version: str
    data_label: str
    honest_labels: list[str]
    labels: dict[str, LabelEntry] = Field(
        description="Central code -> display label dictionary; the UI never shows raw codes"
    )
    kpis: SummaryKpis
    reconciliation: list[ReconciliationCheck]
    residuals: list[Residual]
    unallocated_settlements: list[UnallocatedSettlement]
    library_warnings: list[LibraryWarning]
    waterfall_overall: list[WaterfallStage]
    waterfall_by_segment: list[GroupWaterfall]
    waterfall_by_customer: list[GroupWaterfall]
    leakage_components_overall: list[LeakageComponent]
    dispersion: DispersionSection
    segments: SegmentsSection
    guidance: list[GuidancePair]
    sensitivity: list[SensitivitySegment]
    opportunity: OpportunitySection
    economics: EconomicsSection
    scenario_grids: list[ScenarioGrid]
    optional_modules: OptionalModules
    validation_results: list[ValidationResult]
    manifest: ManifestInfo
