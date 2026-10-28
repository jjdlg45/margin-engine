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
        description="After returns, like every headline KPI - never exceeds net invoiced revenue"
    )
    pocket_gross_profit_cents: int = Field(description="After returns, cost-known lines")
    contribution_cents: int = Field(description="After returns, cost-known lines")
    pocket_margin_pct: float | None = Field(
        default=None, description="Pocket GP / pocket revenue on cost-known lines"
    )
    reconciliation_pass: bool
    missing_cost_revenue_share: float
    list_price_coverage_share: float
    # Run 6, Phase 64 (honest headline): the visible residuals that EXPLAIN
    # dollars the engine would not force onto lines, shown next to the
    # reconciliation status instead of implying everything allocated cleanly.
    n_visible_residuals: int = Field(
        default=0, description="Nonzero reconciliation residuals kept visible"
    )
    visible_residual_cents: int = Field(
        default=0, description="Sum of absolute visible residual amounts"
    )


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
    name: str = Field(description="Plain-English name derived from the rule; covers every branch")
    plain: str = Field(description="One-sentence description derived from the rule")
    conditions: list[str] = Field(
        description="Plain-English bullet list of the rule's conditions, covering "
        "every branch of a merged segment ('How this group is defined'). Raw rule "
        "logic lives only in the CSV/Parquet exports."
    )
    technical_note: str = Field(description="Merge/leaf mechanics for the expander")
    meaning: str | None = Field(
        default=None,
        description="One-line 'what this means' summary generated from the group's "
        "own numbers (margin drivers)",
    )
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


class CompliancePair(BaseModel):
    """One Robinson-Patman review flag, with the facts beside it (Phase 79).

    A review prompt, never a finding: `pair_basis` says whether a reviewer
    confirmed that these two customers compete or whether the attribute
    screen merely suggested it.
    """

    item_id: str
    customer_id_a: str
    customer_id_b: str
    pair_basis: str
    recommended_price_a_cents: int | None
    recommended_price_b_cents: int | None
    recommended_net_a_cents: int | None
    recommended_net_b_cents: int | None
    price_difference_pct: float | None
    net_difference_pct: float | None
    quantity_a: float | None
    quantity_b: float | None
    channel_a: str | None
    channel_b: str | None
    deduction_bp_a: int | None
    deduction_bp_b: int | None
    cts_per_unit_a_cents: int | None
    cts_per_unit_b_cents: int | None
    cts_difference_cents: int | None
    cts_method: str | None
    data_coverage: str
    meeting_competition_record: str | None
    reviewer_decision: str | None


class ComplianceSection(BaseModel):
    screen_attributes: list[str]
    n_screened: int
    n_unscreened: int
    pairs: list[CompliancePair]
    notes: list[str]


class SegmentHoldout(BaseModel):
    """Run 6, Phase 76: how the segmentation does on periods it never saw."""

    n_fit_periods: int
    n_eval_periods: int
    n_eval_pairs: int
    mae: float | None
    mae_baseline: float | None
    mae_without_same_period_features: float | None
    same_period_features: list[str]
    note: str


class SegmentsSection(BaseModel):
    rules: list[SegmentRule]
    margin_driver_rules: list[SegmentRule]
    margin_driver_note: str
    bootstrap_median_ari: float | None
    recommendation_change_share: float | None
    index_distribution: list[SegmentIndexStats]
    holdout: SegmentHoldout | None = None
    notes: list[str] = []


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
    # Run 4 (24.3/24.4): both floors, which one binds, and list provenance.
    invoice_floor_gross_cents: int | None = None
    invoice_contribution_floor_cents: int | None = None
    binding_floor_basis: str | None = None
    list_provenance: str | None = None
    target_index: float | None
    target_label: str | None
    status: str
    annual_net_revenue_cents: int | None = None
    explanation: str


class CausalGate(BaseModel):
    """One causal criterion's outcome (Run 6, Phase 70). Machine-readable so
    the UI renders a checklist instead of parsing prose."""

    order: int
    gate: str
    result: str  # pass | fail | not_evaluated
    detail: str


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
    # Run 4: weak-IV boundedness (24.7), identifying price support (24.6),
    # carried-forward robustness (24.8).
    ar_bounded: bool | None = None
    support_log_lo: float | None = None
    support_log_hi: float | None = None
    # Run 6, Phase 74: where inside those bounds the movement is dense enough
    # to inform a response, and how the observed spread compares.
    support_n: int | None = None
    support_regions: str | None = None
    support_n_regions: int | None = None
    support_window: float | None = None
    support_min_observations: int | None = None
    observed_log_lo: float | None = None
    observed_log_hi: float | None = None
    support_detail: str | None = None
    ppml_positive: float | None = None
    carried_forward_share: float | None = None
    imputed_price_share: float | None = None
    # Total-quantity estimand (Run 6, Phase 72), reported beside the
    # conditional estimate and never above it: its ladder has no causal tier.
    iv_ppml: float | None = None
    iv_ppml_status: str | None = None
    iv_ppml_reasons: str | None = None
    iv_ppml_first_stage_f: float | None = None
    iv_ppml_n_rows: int | None = None
    price_source_mix: str | None = None
    # Two standard errors, because they cover different things: the analytic
    # one is cluster-robust but treats the estimated control function as known,
    # and the resampled one refits the first stage per replicate but cannot
    # resample the period level the instrument is assigned at. `inference`
    # spells out what each omits; neither is presented as the whole answer.
    iv_ppml_se_analytic: float | None = None
    iv_ppml_se_bootstrap: float | None = None
    iv_ppml_range_low: float | None = None
    iv_ppml_range_high: float | None = None
    iv_ppml_inference: str | None = None
    # Half-panel jackknife bias diagnostic: reported, never adopted.
    iv_ppml_spj: float | None = None
    iv_ppml_spj_bias: float | None = None
    iv_ppml_spj_detail: str | None = None
    # Rows and units that never reached the fit, by reason.
    iv_ppml_sample_losses: str | None = None
    iv_ppml_rows_dropped: int | None = None
    iv_ppml_units_dropped: int | None = None
    # Largest move under an alternative zero-row price or availability
    # assumption, and the account of what was varied. Past the configured gap
    # the estimand is demoted, so this is the reason behind a status.
    iv_ppml_zero_row_gap: float | None = None
    iv_ppml_zero_row_detail: str | None = None
    market_control_used: bool = False
    # Run 6, Phase 70: the named estimand, the market control's adequacy
    # statistics, the competitor-price path resolution and the per-gate
    # record. All optional so an older bundle stays loadable.
    estimand: str | None = None
    estimand_bias_direction: str | None = None
    market_control_status: str | None = None
    market_control_coverage: float | None = None
    market_control_lag_periods: int | None = None
    market_control_within_cell_values: float | None = None
    market_control_price_r2: float | None = None
    market_control_instrument_r2: float | None = None
    market_control_detail: str | None = None
    competitor_path_resolution: str | None = None
    competitor_path_detail: str | None = None
    first_stage_f_uncontrolled: float | None = None
    n_identified_instruments: int | None = None
    robustness_n_specifications: int | None = None
    robustness_excluded_weak: str | None = None
    # Run 6, Phase 71: the robust first-stage statistic and the shape of the
    # weak-instrument-robust set (a HULL, not a confidence interval).
    effective_first_stage_f: float | None = None
    effective_k: float | None = None
    n_absorbed_parameters: int | None = None
    n_clusters: int | None = None
    effective_f_detail: str | None = None
    # Run 6, Phase 73: the cluster structure inference rests on, and the
    # remedy used when the assignment level leaves too few clusters.
    cluster_level_used: str | None = None
    n_clusters_by_dimension: str | None = None
    effective_clusters: float | None = None
    n_clusters_with_instrument_variation: int | None = None
    max_cluster_variation_share: float | None = None
    cluster_variation_detail: str | None = None
    small_cluster_method_used: str | None = None
    small_cluster_detail: str | None = None
    primary_interval_basis: str | None = None
    analytic_ci_low: float | None = None
    analytic_ci_high: float | None = None
    wild_ci_low: float | None = None
    wild_ci_high: float | None = None
    wild_bootstrap_reps_used: int | None = None
    ar_shape: str | None = None
    ar_hull_lo: float | None = None
    ar_hull_hi: float | None = None
    ar_measure: float | None = None
    ar_n_components: int | None = None
    ar_display_reliable: bool | None = None
    ar_detail: str | None = None
    lead_test_status: str | None = None
    lead_test_p: float | None = None
    lead_test_horizon: int | None = None
    lead_test_bound: float | None = None
    lead_test_detail: str | None = None
    # Shuffled-instrument placebo, annotated when the validation suite has run.
    placebo_status: str = "placebo_not_run"
    placebo_share_significant: float | None = None
    placebo_actual_p: float | None = None
    placebo_detail: str | None = None
    causal_gates: list[CausalGate] = Field(default_factory=list)
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
    gross_price_opportunity_conservative_cents: int | None = None
    # Two expected-change bases (Run 6, Phase 65), never conflated: the pocket
    # gross profit change (P(1-d) - F - C economics) and the contribution
    # change (also subtracts estimated per-unit cost-to-serve K; null when no
    # K is supported for the pair).
    expected_pocket_gross_profit_change_cents: int | None = None
    expected_contribution_change_cents: int | None
    volume_treatment: str
    # How post-invoice deductions were treated for this record (Run 6, Phase
    # 66): exact flat rate, recomputed tiered formula, or a labeled
    # fixed-rate approximation.
    deduction_treatment: str = "flat_rate_exact"
    cost_unavailable: bool = False
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
    gross_conservative_total_cents: int | None = None
    # Pocket gross profit basis total (Run 6, Phase 65), beside the
    # contribution-basis total - two metrics, never conflated or summed.
    expected_gp_total_cents: int = 0
    expected_total_cents: int
    expected_total_note: str
    optimizer_objective: str = "pocket_gross_profit"
    n_cost_unavailable: int = 0
    n_outside_price_support: int = 0
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
    # Run 6, Phase 77: what was modeled, how it was clustered, and whether its
    # probabilities held up on quotes it had not seen.
    n_won: int = 0
    n_lost: int = 0
    cluster_level: str = "none"
    n_clusters: int | None = None
    controls: str = ""
    holdout_n: int = 0
    calibration_slope: float | None = None
    calibration_intercept: float | None = None
    brier: float | None = None
    auc: float | None = None
    calibration_status: str = "not testable"


class QuoteExclusion(BaseModel):
    reason: str
    n: int


class QuoteIntake(BaseModel):
    """Run 6, Phase 77: every quote row, and where it went."""

    n_rows: int
    unit: str
    n_modeled: int
    excluded: list[QuoteExclusion]
    notes: list[str]


class CrossPriceEstimate(BaseModel):
    group_id: str
    item_a: str
    item_b: str
    n_obs: int
    cross_elasticity: float | None
    cross_se: float | None
    status: str
    reasons: str
    # Both first-stage statistics travel with the estimate: the marginal F says
    # the instruments move each price, the conditional one says they move one
    # price independently of the other. Only the second gates the status.
    marginal_first_stage_f: float | None = None
    conditional_effective_f: float | None = None
    n_clusters: int | None = None
    n_absorbed_parameters: int | None = None
    first_stage_detail: str = ""


class InventorySection(BaseModel):
    method: str
    n_sales_costed: int
    n_flagged: int
    cogs_total_cents: int
    timing_statement: str


class OptionalModules(BaseModel):
    win_curves: list[WinCurve] | None
    win_curves_absent_reason: str | None
    quote_intake: QuoteIntake | None = None
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


class ActionEventRow(BaseModel):
    """One append-only lifecycle event (Phase 38); the static demo ships a
    simulated history, labeled demo data."""

    seq: int
    who: str
    at: str = Field(description="ISO datetime, UTC")
    from_status: str | None
    to_status: str
    note: str


class ActionOutcome(BaseModel):
    """Measured result of an implemented action (Phase 41). Every gate fails
    closed: outcome_status insufficient_evidence carries the reason."""

    implemented_on: str
    window_days: int
    qty_before: float | None
    qty_after: float | None
    price_before_cents: int | None
    price_after_cents: int | None
    proposed_price_cents: int | None
    realization_pct: float | None
    predicted_volume_ratio: float | None
    treated_volume_ratio: float | None
    control_volume_ratio: float | None
    n_control_pairs: int
    control_adjusted_volume_ratio: float | None
    # Run 6, Phase 75: a pair that stopped buying is a result, not a gap.
    stopped_buying: bool | None = None
    n_control_pairs_excluded: int | None = None
    control_exclusions: str | None = None
    wave: str | None = None
    implementation_compliance: str | None = None
    volume_error_log_points: float | None
    pocket_margin_before: float | None
    pocket_margin_after: float | None
    contribution_before_cents: int | None
    contribution_after_cents: int | None
    outcome_status: str
    outcome_reason: str


class ActionRow(BaseModel):
    """One generated action with its derived status, full event history and
    outcome (once measured). Impact levels are per impact_basis and never
    summed across bases."""

    action_id: str
    action_type: str
    title: str
    rationale: str
    customer_id: str | None
    customer_name: str | None = None
    item_id: str | None
    item_name: str | None = None
    scope: str
    uom: str | None
    segment: int | None
    guidance_status: str | None
    evidence_status: str | None
    volume_treatment: str | None
    predicted_volume_ratio: float | None
    impact_basis: str
    expected_impact_cents: int | None
    current_price_cents: int | None
    proposed_price_cents: int | None
    floor_price_cents: int | None
    pass_through_level: float | None = Field(
        default=None, description="cost_pass_through only: the chosen pass-through level"
    )
    incremental_cost_cents: int | None = Field(
        default=None, description="cost_pass_through only: the scenario's incremental cost"
    )
    priority: str
    owner: str
    due_date: str | None
    overdue: bool = Field(
        default=False,
        description="True when the status still needs someone to act and the "
        "due date lies before the data's as-of date",
    )
    compliance_flags: str
    blocked: bool
    blocked_reason: str | None
    status: str
    events: list[ActionEventRow]
    outcome: ActionOutcome | None = None


class CalibrationRow(BaseModel):
    """Prediction error by evidence bucket; below the configured minimum the
    status says not_enough_implemented_actions instead of showing numbers."""

    bucket: str
    n_measured: int
    median_abs_volume_error_log_points: float | None
    median_realization_pct: float | None
    status: str
    # Run 6, Phase 75: pairs that stopped buying are IN the count and the
    # medians above; their volume error is undefined, so they are named.
    n_stopped_buying: int | None = None


class ResultsBreakdownRow(BaseModel):
    """Measured results split by wave, segment, action type, evidence and how
    faithfully the price actually moved (Run 6, Phase 75). One pooled median
    hides where a number came from."""

    dimension: str
    value: str
    n_measured: int
    n_stopped_buying: int
    median_control_adjusted_volume_ratio: float | None
    median_realization_pct: float | None


class EventStudyCoefficient(BaseModel):
    event_time: int
    coefficient_pct: float
    se_pct: float | None
    ci_low_pct: float | None
    ci_high_pct: float | None
    n_treated_pairs: int
    period: str


class EventStudySection(BaseModel):
    """The pooled estimate, separate from the per-action control-adjusted
    changes because it rests on assumptions they do not (Run 6, Phase 75)."""

    status: str
    reason: str
    estimand: str
    att_pct: float | None = None
    att_se: float | None = None
    att_ci_low: float | None = None
    att_ci_high: float | None = None
    coefficients: list[EventStudyCoefficient] = Field(default_factory=list)
    pretrend_status: str
    pretrend_detail: str
    n_treated_pairs: int
    n_control_pairs: int
    n_cohorts: int
    n_rows: int


class ActionsSummary(BaseModel):
    n_actions: int
    by_status: dict[str, int]
    impact_by_evidence_cents: dict[str, int]
    impact_by_basis_cents: dict[str, int] = Field(
        description="Expected impact per impact basis - bases are different "
        "opportunity levels and are NEVER summed into one number"
    )
    n_blocked: int
    n_compliance_flagged: int
    n_measured: int
    median_realization_pct: float | None = Field(
        default=None, description="Across measured outcomes; null below the minimum count"
    )


class OverlapGroup(BaseModel):
    """Overlapping action scopes (Run 6, Phase 68): the naive sum is shown
    only to make double counting visible; the portfolio estimate is the joint
    figure under one baseline and is NEVER added to the single-action lists."""

    overlap_group: str
    n_actions: int
    action_ids: list[str]
    naive_sum_cents: int
    portfolio_estimate_cents: int
    note: str


class ActionsSection(BaseModel):
    read_only: bool = Field(
        description="True in the static demo: the review workflow needs the API"
    )
    demo_history: bool = Field(description="True when the event history is simulated demo data")
    summary: ActionsSummary
    calibration: list[CalibrationRow]
    # Run 6, Phase 75: the breakdowns and the pooled estimate.
    results_breakdown: list[ResultsBreakdownRow] = Field(default_factory=list)
    event_study: EventStudySection | None = None
    facets: dict[str, list[str]]
    overlap_groups: list[OverlapGroup] = Field(default_factory=list)
    notes: list[str]


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
    actions: ActionsSection
    actions_table: list[ActionRow]
    compliance: ComplianceSection | None = None
