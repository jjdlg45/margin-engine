/* Generated from schema.json (margin-engine export-web). Do not edit. */

export type Bucket = string;
export type MedianAbsVolumeErrorLogPoints = number | null;
export type MedianRealizationPct = number | null;
export type NMeasured = number;
export type Status = string;
export type Calibration = CalibrationRow[];
/**
 * True when the event history is simulated demo data
 */
export type DemoHistory = boolean;
export type Notes = string[];
export type ActionIds = string[];
export type NActions = number;
export type NaiveSumCents = number;
export type Note = string;
export type OverlapGroup1 = string;
export type PortfolioEstimateCents = number;
export type OverlapGroups = OverlapGroup[];
/**
 * True in the static demo: the review workflow needs the API
 */
export type ReadOnly = boolean;
/**
 * Across measured outcomes; null below the minimum count
 */
export type MedianRealizationPct1 = number | null;
export type NActions1 = number;
export type NBlocked = number;
export type NComplianceFlagged = number;
export type NMeasured1 = number;
export type ActionId = string;
export type ActionType = string;
export type Blocked = boolean;
export type BlockedReason = string | null;
export type ComplianceFlags = string;
export type CurrentPriceCents = number | null;
export type CustomerId = string | null;
export type CustomerName = string | null;
export type DueDate = string | null;
/**
 * ISO datetime, UTC
 */
export type At = string;
export type FromStatus = string | null;
export type Note1 = string;
export type Seq = number;
export type ToStatus = string;
export type Who = string;
export type Events = ActionEventRow[];
export type EvidenceStatus = string | null;
export type ExpectedImpactCents = number | null;
export type FloorPriceCents = number | null;
export type GuidanceStatus = string | null;
export type ImpactBasis = string;
/**
 * cost_pass_through only: the scenario's incremental cost
 */
export type IncrementalCostCents = number | null;
export type ItemId = string | null;
export type ItemName = string | null;
export type ContributionAfterCents = number | null;
export type ContributionBeforeCents = number | null;
export type ControlVolumeRatio = number | null;
export type DidVolumeRatio = number | null;
export type ImplementedOn = string;
export type NControlPairs = number;
export type OutcomeReason = string;
export type OutcomeStatus = string;
export type PocketMarginAfter = number | null;
export type PocketMarginBefore = number | null;
export type PredictedVolumeRatio = number | null;
export type PriceAfterCents = number | null;
export type PriceBeforeCents = number | null;
export type ProposedPriceCents = number | null;
export type QtyAfter = number | null;
export type QtyBefore = number | null;
export type RealizationPct = number | null;
export type TreatedVolumeRatio = number | null;
export type VolumeErrorLogPoints = number | null;
export type WindowDays = number;
/**
 * True when the status still needs someone to act and the due date lies before the data's as-of date
 */
export type Overdue = boolean;
export type Owner = string;
/**
 * cost_pass_through only: the chosen pass-through level
 */
export type PassThroughLevel = number | null;
export type PredictedVolumeRatio1 = number | null;
export type Priority = string;
export type ProposedPriceCents1 = number | null;
export type Rationale = string;
export type Scope = string;
export type Segment = number | null;
export type Status1 = string;
export type Title = string;
export type Uom = string | null;
export type VolumeTreatment = string | null;
export type ActionsTable = ActionRow[];
export type BundleVersion = string;
export type DataLabel = string;
export type FlagNote = string;
export type NCommercialOutlierFlags = number;
export type NDataQualityFlags = number;
export type NPeerGroups = number;
export type Iqr = number;
export type MeanDiscountToList = number;
export type MedianIndex = number;
export type NCommercialOutlierFlags1 = number;
export type NDataQualityFlags1 = number;
export type NObservations = number;
export type P10 = number;
export type P90 = number;
export type PeerGroup = string;
export type TopGroups = PeerGroupStat[];
export type ActivityCtsCents = number;
export type AvgOrderValueCents = number;
export type ContributionAfterActivityCents = number;
export type ContributionCents = number;
export type CustomerId1 = string;
export type CustomerName1 = string | null;
export type NOrders = number;
export type NetRevenueCents = number;
export type PctRevenueExcluded = number;
export type ReturnsContributionCents = number;
export type SalesContributionCents = number;
export type ServiceFlags = string[];
export type Customers = CustomerEconomics[];
export type ActivityStatusNote = string;
export type MedianOrderValueCents = number;
export type MinOrderValueCents = number;
export type NOrders1 = number;
export type NegativeContributionOrders = number;
export type OrdersBelowMinValue = number;
export type EngineVersion = string;
export type AnnualNetRevenueCents = number | null;
export type BandP25 = number | null;
export type BandP50 = number | null;
export type BandP75 = number | null;
export type BindingFloorBasis = string | null;
export type CurrentIndex = number | null;
export type CustomerId2 = string;
export type CustomerName2 = string | null;
export type Explanation = string;
export type FloorIndex = number | null;
export type InvoiceContributionFloorCents = number | null;
export type InvoiceFloorCents = number | null;
export type InvoiceFloorGrossCents = number | null;
export type ItemId1 = string;
export type ItemName1 = string | null;
export type ListProvenance = string | null;
export type PocketFloorCents = number | null;
export type Segment1 = number | null;
export type Status2 = string;
export type TargetIndex = number | null;
export type TargetLabel = string | null;
export type Guidance = GuidancePair[];
export type HonestLabels = string[];
/**
 * After returns, cost-known lines
 */
export type ContributionCents1 = number;
/**
 * Sale lines only, before returns
 */
export type GrossSalesCents = number;
export type ListPriceCoverageShare = number;
export type MissingCostRevenueShare = number;
export type MonthsCovered = number;
export type NCustomers = number;
export type NItems = number;
export type NLedgerLines = number;
export type NSalesObservations = number;
/**
 * Nonzero reconciliation residuals kept visible
 */
export type NVisibleResiduals = number;
/**
 * Gross sales plus returns - matches economics
 */
export type NetRevenueCents1 = number;
/**
 * After returns, cost-known lines
 */
export type PocketGrossProfitCents = number;
/**
 * Pocket GP / pocket revenue on cost-known lines
 */
export type PocketMarginPct = number | null;
/**
 * After returns, like every headline KPI - never exceeds net invoiced revenue
 */
export type PocketRevenueCents = number;
export type ReconciliationPass = boolean;
/**
 * Linked return lines (negative)
 */
export type ReturnsCents = number;
/**
 * Sum of absolute visible residual amounts
 */
export type VisibleResidualCents = number;
/**
 * Time window every KPI covers, e.g. '24 months'
 */
export type WindowLabel = string;
export type Description = string;
export type Label = string;
export type AmountCents = number;
export type Component = string;
export type ShareOfLeakage = number | null;
export type LeakageComponentsOverall = LeakageComponent[];
export type Category = string;
export type Message = string;
export type NOccurrences = number;
export type LibraryWarnings = LibraryWarning[];
export type InputFiles = {
  [k: string]: string;
}[];
export type MappingSha256 = string;
export type NoteOnVolatileFields = string;
export type Notes1 = string[];
export type RulesSha256 = string;
export type ExpectedGpTotalCents = number;
export type ExpectedTotalCents = number;
export type ExpectedTotalNote = string;
export type GrossConservativeTotalCents = number | null;
export type GrossTotalCents = number;
export type Components = LeakageComponent[];
export type CustomerId3 = string;
export type NetRevenueCents2 = number;
export type TotalLeakageCents = number;
export type LeakageByCustomer = CustomerLeakage[];
export type LeakageTotalCents = number;
export type NCostUnavailable = number;
export type NOutsidePriceSupport = number;
export type NRecords = number;
export type NeverSummedNote = string;
export type OptimizerObjective = string;
export type CostUnavailable = boolean;
export type CustomerId4 = string;
export type CustomerName3 = string | null;
export type DeductionTreatment = string;
export type EvidenceStatus1 = string | null;
export type ExpectedContributionChangeCents = number | null;
export type ExpectedPocketGrossProfitChangeCents = number | null;
export type Explanation1 = string;
export type GrossPriceOpportunityCents = number;
export type GrossPriceOpportunityConservativeCents = number | null;
export type ItemId2 = string;
export type ItemName2 = string | null;
/**
 * (target - current) / target, from the pair's guidance
 */
export type PctBelowTarget = number | null;
export type Segment2 = number | null;
/**
 * One short row-specific sentence for the table
 */
export type Summary = string;
export type VolumeTreatment1 = string;
export type TopOpportunities = OpportunityRecord[];
/**
 * Baseline window for every figure in this section
 */
export type WindowLabel1 = string;
export type CrossPrice = CrossPriceEstimate[] | null;
export type CrossElasticity = number | null;
export type CrossSe = number | null;
export type GroupId = string;
export type ItemA = string;
export type ItemB = string;
export type NObs = number;
export type Reasons = string;
export type Status3 = string;
export type CrossPriceAbsentReason = string | null;
export type CogsTotalCents = number;
export type Method = string;
export type NFlagged = number;
export type NSalesCosted = number;
export type TimingStatement = string;
export type InventoryAbsentReason = string | null;
export type WinCurves = WinCurve[] | null;
export type CiHigh = number | null;
export type CiLow = number | null;
export type NQuotes = number;
export type NegotiationGap = number | null;
export type PriceCoef = number | null;
export type PriceCoefSe = number | null;
export type Reasons1 = string;
export type Segment3 = number;
export type Status4 = string;
export type WinRate = number;
export type WinCurvesAbsentReason = string | null;
export type ActualCents = number;
export type AllocatedCents = number | null;
export type Check = string;
export type ExpectedCents = number;
export type Passed = boolean;
export type ResidualCents = number | null;
export type SourceTotalCents = number | null;
export type Reconciliation = ReconciliationCheck[];
export type AmountCents1 = number;
export type Name = string;
export type What = string;
export type WhereListed = string;
export type WhyNotAllocated = string;
export type Residuals = Residual[];
export type CogsTiming = string;
export type NPairs = number;
export type Name1 = string;
export type PctIncrease = number;
export type ContributionEffectCents = number | null;
export type ContributionEffectSupportedEverywhere = boolean;
export type IncrementalCostCents1 = number;
export type PassThrough = number;
export type ProtectedExposureCents = number;
export type RecoveredCents = number;
export type ResidualExposureCents = number;
export type Points = ScenarioGridPoint[];
export type Scope1 = string;
/**
 * Supplier/category display name
 */
export type ScopeLabel = string | null;
export type ScopeValue = string;
export type ScenarioGrids = ScenarioGrid[];
export type BootstrapMedianAri = number | null;
export type NPairs1 = number;
export type P25 = number;
export type P50 = number;
export type P75 = number;
export type Segment4 = number;
export type IndexDistribution = SegmentIndexStats[];
export type MarginDriverNote = string;
/**
 * Plain-English bullet list of the rule's conditions, covering every branch of a merged segment ('How this group is defined'). Raw rule logic lives only in the CSV/Parquet exports.
 */
export type Conditions = string[];
/**
 * 1-based rank by size; viewers see 'Segment {rank}'
 */
export type DisplayRank = number;
/**
 * Tree node id - technical detail only
 */
export type LeafId = number;
export type MeanTarget = number;
/**
 * One-line 'what this means' summary generated from the group's own numbers (margin drivers)
 */
export type Meaning = string | null;
export type NCustomers1 = number;
export type NItems1 = number;
export type NLines = number;
/**
 * Plain-English name derived from the rule; covers every branch
 */
export type Name2 = string;
/**
 * One-sentence description derived from the rule
 */
export type Plain = string;
/**
 * Merge/leaf mechanics for the expander
 */
export type TechnicalNote = string;
export type MarginDriverRules = SegmentRule[];
export type RecommendationChangeShare = number | null;
export type Rules = SegmentRule[];
export type ArBounded = boolean | null;
export type ArCiHigh = number | null;
export type ArCiLow = number | null;
export type CarriedForwardShare = number | null;
export type Detail = string;
export type Gate = string;
export type Order = number;
export type Result = string;
export type CausalGates = CausalGate[];
export type CompetitorPathDetail = string | null;
export type CompetitorPathResolution = string | null;
export type Estimand = string | null;
export type EstimandBiasDirection = string | null;
export type FirstStageF = number | null;
export type FirstStageFUncontrolled = number | null;
export type IdentifyingCoverage = number | null;
export type Iv = number | null;
export type IvCiHigh = number | null;
export type IvCiLow = number | null;
export type IvSe = number | null;
export type MarketControlCoverage = number | null;
export type MarketControlDetail = string | null;
export type MarketControlInstrumentR2 = number | null;
export type MarketControlLagPeriods = number | null;
export type MarketControlPriceR2 = number | null;
export type MarketControlStatus = string | null;
export type MarketControlUsed = boolean;
export type MarketControlWithinCellValues = number | null;
export type NIdentifiedInstruments = number | null;
export type NObs1 = number;
export type NPairs2 = number;
export type NPositive = number;
export type NaiveFeOls = number | null;
export type NaiveSe = number | null;
export type NaiveVsIvNote = string;
export type OveridJ = number | null;
export type OveridP = number | null;
export type Ppml = number | null;
export type PpmlPositive = number | null;
export type PriceCvWithin = number;
export type RobustnessExcludedWeak = string | null;
export type RobustnessNSpecifications = number | null;
export type RobustnessSpread = number | null;
export type Segment5 = number;
export type Status5 = string;
export type StatusReasons = string;
export type SupportLogHi = number | null;
export type SupportLogLo = number | null;
export type Sensitivity = SensitivitySegment[];
export type AmountCents2 = number;
export type CustomerId5 = string;
export type Period = string;
export type RebateProgramId = string;
export type UnallocatedSettlements = UnallocatedSettlement[];
export type Achieved = string;
export type Check1 = string;
export type Passed1 = boolean;
export type Target = string;
export type ValidationResults = ValidationResult[];
/**
 * Segment id or customer id
 */
export type Key = string;
export type Label1 = string;
export type NetRevenueCents3 = number;
export type AmountCents3 = number;
export type Coverage = string;
/**
 * None when no list coverage
 */
export type PctOfList = number | null;
export type Stage = string;
export type Stages = WaterfallStage[];
export type WaterfallByCustomer = GroupWaterfall[];
export type WaterfallBySegment = GroupWaterfall[];
export type WaterfallOverall = WaterfallStage[];

export interface WebBundle {
  actions: ActionsSection;
  actions_table: ActionsTable;
  bundle_version: BundleVersion;
  data_label: DataLabel;
  dispersion: DispersionSection;
  economics: EconomicsSection;
  engine_version: EngineVersion;
  guidance: Guidance;
  honest_labels: HonestLabels;
  kpis: SummaryKpis;
  labels: Labels;
  leakage_components_overall: LeakageComponentsOverall;
  library_warnings: LibraryWarnings;
  manifest: ManifestInfo;
  opportunity: OpportunitySection;
  optional_modules: OptionalModules;
  reconciliation: Reconciliation;
  residuals: Residuals;
  scenario_grids: ScenarioGrids;
  segments: SegmentsSection;
  sensitivity: Sensitivity;
  unallocated_settlements: UnallocatedSettlements;
  validation_results: ValidationResults;
  waterfall_by_customer: WaterfallByCustomer;
  waterfall_by_segment: WaterfallBySegment;
  waterfall_overall: WaterfallOverall;
}
export interface ActionsSection {
  calibration: Calibration;
  demo_history: DemoHistory;
  facets: Facets;
  notes: Notes;
  overlap_groups?: OverlapGroups;
  read_only: ReadOnly;
  summary: ActionsSummary;
}
/**
 * Prediction error by evidence bucket; below the configured minimum the
 * status says not_enough_implemented_actions instead of showing numbers.
 */
export interface CalibrationRow {
  bucket: Bucket;
  median_abs_volume_error_log_points: MedianAbsVolumeErrorLogPoints;
  median_realization_pct: MedianRealizationPct;
  n_measured: NMeasured;
  status: Status;
}
export interface Facets {
  [k: string]: string[];
}
/**
 * Overlapping action scopes (Run 6, Phase 68): the naive sum is shown
 * only to make double counting visible; the portfolio estimate is the joint
 * figure under one baseline and is NEVER added to the single-action lists.
 */
export interface OverlapGroup {
  action_ids: ActionIds;
  n_actions: NActions;
  naive_sum_cents: NaiveSumCents;
  note: Note;
  overlap_group: OverlapGroup1;
  portfolio_estimate_cents: PortfolioEstimateCents;
}
export interface ActionsSummary {
  by_status: ByStatus;
  impact_by_basis_cents: ImpactByBasisCents;
  impact_by_evidence_cents: ImpactByEvidenceCents;
  median_realization_pct?: MedianRealizationPct1;
  n_actions: NActions1;
  n_blocked: NBlocked;
  n_compliance_flagged: NComplianceFlagged;
  n_measured: NMeasured1;
}
export interface ByStatus {
  [k: string]: number;
}
/**
 * Expected impact per impact basis - bases are different opportunity levels and are NEVER summed into one number
 */
export interface ImpactByBasisCents {
  [k: string]: number;
}
export interface ImpactByEvidenceCents {
  [k: string]: number;
}
/**
 * One generated action with its derived status, full event history and
 * outcome (once measured). Impact levels are per impact_basis and never
 * summed across bases.
 */
export interface ActionRow {
  action_id: ActionId;
  action_type: ActionType;
  blocked: Blocked;
  blocked_reason: BlockedReason;
  compliance_flags: ComplianceFlags;
  current_price_cents: CurrentPriceCents;
  customer_id: CustomerId;
  customer_name?: CustomerName;
  due_date: DueDate;
  events: Events;
  evidence_status: EvidenceStatus;
  expected_impact_cents: ExpectedImpactCents;
  floor_price_cents: FloorPriceCents;
  guidance_status: GuidanceStatus;
  impact_basis: ImpactBasis;
  incremental_cost_cents?: IncrementalCostCents;
  item_id: ItemId;
  item_name?: ItemName;
  outcome?: ActionOutcome | null;
  overdue?: Overdue;
  owner: Owner;
  pass_through_level?: PassThroughLevel;
  predicted_volume_ratio: PredictedVolumeRatio1;
  priority: Priority;
  proposed_price_cents: ProposedPriceCents1;
  rationale: Rationale;
  scope: Scope;
  segment: Segment;
  status: Status1;
  title: Title;
  uom: Uom;
  volume_treatment: VolumeTreatment;
}
/**
 * One append-only lifecycle event (Phase 38); the static demo ships a
 * simulated history, labeled demo data.
 */
export interface ActionEventRow {
  at: At;
  from_status: FromStatus;
  note: Note1;
  seq: Seq;
  to_status: ToStatus;
  who: Who;
}
/**
 * Measured result of an implemented action (Phase 41). Every gate fails
 * closed: outcome_status insufficient_evidence carries the reason.
 */
export interface ActionOutcome {
  contribution_after_cents: ContributionAfterCents;
  contribution_before_cents: ContributionBeforeCents;
  control_volume_ratio: ControlVolumeRatio;
  did_volume_ratio: DidVolumeRatio;
  implemented_on: ImplementedOn;
  n_control_pairs: NControlPairs;
  outcome_reason: OutcomeReason;
  outcome_status: OutcomeStatus;
  pocket_margin_after: PocketMarginAfter;
  pocket_margin_before: PocketMarginBefore;
  predicted_volume_ratio: PredictedVolumeRatio;
  price_after_cents: PriceAfterCents;
  price_before_cents: PriceBeforeCents;
  proposed_price_cents: ProposedPriceCents;
  qty_after: QtyAfter;
  qty_before: QtyBefore;
  realization_pct: RealizationPct;
  treated_volume_ratio: TreatedVolumeRatio;
  volume_error_log_points: VolumeErrorLogPoints;
  window_days: WindowDays;
}
export interface DispersionSection {
  abc_revenue_counts: AbcRevenueCounts;
  flag_note: FlagNote;
  n_commercial_outlier_flags: NCommercialOutlierFlags;
  n_data_quality_flags: NDataQualityFlags;
  n_peer_groups: NPeerGroups;
  top_groups: TopGroups;
}
export interface AbcRevenueCounts {
  [k: string]: number;
}
export interface PeerGroupStat {
  iqr: Iqr;
  mean_discount_to_list: MeanDiscountToList;
  median_index: MedianIndex;
  n_commercial_outlier_flags: NCommercialOutlierFlags1;
  n_data_quality_flags: NDataQualityFlags1;
  n_observations: NObservations;
  p10: P10;
  p90: P90;
  peer_group: PeerGroup;
}
export interface EconomicsSection {
  customers: Customers;
  orders: OrderEconomicsSummary;
}
export interface CustomerEconomics {
  activity_cts_cents: ActivityCtsCents;
  avg_order_value_cents: AvgOrderValueCents;
  contribution_after_activity_cents: ContributionAfterActivityCents;
  contribution_cents: ContributionCents;
  customer_id: CustomerId1;
  customer_name?: CustomerName1;
  n_orders: NOrders;
  net_revenue_cents: NetRevenueCents;
  pct_revenue_excluded: PctRevenueExcluded;
  returns_contribution_cents: ReturnsContributionCents;
  sales_contribution_cents: SalesContributionCents;
  service_flags: ServiceFlags;
}
export interface OrderEconomicsSummary {
  activity_status_note: ActivityStatusNote;
  median_order_value_cents: MedianOrderValueCents;
  min_order_value_cents: MinOrderValueCents;
  n_orders: NOrders1;
  negative_contribution_orders: NegativeContributionOrders;
  orders_below_min_value: OrdersBelowMinValue;
}
export interface GuidancePair {
  annual_net_revenue_cents?: AnnualNetRevenueCents;
  band_p25: BandP25;
  band_p50: BandP50;
  band_p75: BandP75;
  binding_floor_basis?: BindingFloorBasis;
  current_index: CurrentIndex;
  customer_id: CustomerId2;
  customer_name?: CustomerName2;
  explanation: Explanation;
  floor_index: FloorIndex;
  invoice_contribution_floor_cents?: InvoiceContributionFloorCents;
  invoice_floor_cents: InvoiceFloorCents;
  invoice_floor_gross_cents?: InvoiceFloorGrossCents;
  item_id: ItemId1;
  item_name?: ItemName1;
  list_provenance?: ListProvenance;
  pocket_floor_cents: PocketFloorCents;
  segment: Segment1;
  status: Status2;
  target_index: TargetIndex;
  target_label: TargetLabel;
}
export interface SummaryKpis {
  contribution_cents: ContributionCents1;
  gross_sales_cents: GrossSalesCents;
  list_price_coverage_share: ListPriceCoverageShare;
  missing_cost_revenue_share: MissingCostRevenueShare;
  months_covered: MonthsCovered;
  n_customers: NCustomers;
  n_items: NItems;
  n_ledger_lines: NLedgerLines;
  n_sales_observations: NSalesObservations;
  n_visible_residuals?: NVisibleResiduals;
  net_revenue_cents: NetRevenueCents1;
  pocket_gross_profit_cents: PocketGrossProfitCents;
  pocket_margin_pct?: PocketMarginPct;
  pocket_revenue_cents: PocketRevenueCents;
  reconciliation_pass: ReconciliationPass;
  returns_cents: ReturnsCents;
  visible_residual_cents?: VisibleResidualCents;
  window_label: WindowLabel;
}
/**
 * Central code -> display label dictionary; the UI never shows raw codes
 */
export interface Labels {
  [k: string]: LabelEntry;
}
export interface LabelEntry {
  description: Description;
  label: Label;
}
export interface LeakageComponent {
  amount_cents: AmountCents;
  component: Component;
  share_of_leakage?: ShareOfLeakage;
}
export interface LibraryWarning {
  category: Category;
  message: Message;
  n_occurrences: NOccurrences;
}
export interface ManifestInfo {
  cleaning_decisions: CleaningDecisions;
  input_files: InputFiles;
  mapping_sha256: MappingSha256;
  note_on_volatile_fields: NoteOnVolatileFields;
  notes: Notes1;
  rules_sha256: RulesSha256;
  software_versions: SoftwareVersions;
}
export interface CleaningDecisions {
  [k: string]: number;
}
export interface SoftwareVersions {
  [k: string]: string;
}
export interface OpportunitySection {
  expected_gp_total_cents?: ExpectedGpTotalCents;
  expected_total_cents: ExpectedTotalCents;
  expected_total_note: ExpectedTotalNote;
  gross_conservative_total_cents?: GrossConservativeTotalCents;
  gross_total_cents: GrossTotalCents;
  leakage_by_customer: LeakageByCustomer;
  leakage_total_cents: LeakageTotalCents;
  n_cost_unavailable?: NCostUnavailable;
  n_outside_price_support?: NOutsidePriceSupport;
  n_records: NRecords;
  never_summed_note: NeverSummedNote;
  optimizer_objective?: OptimizerObjective;
  top_opportunities: TopOpportunities;
  window_label: WindowLabel1;
}
export interface CustomerLeakage {
  components: Components;
  customer_id: CustomerId3;
  net_revenue_cents: NetRevenueCents2;
  total_leakage_cents: TotalLeakageCents;
}
export interface OpportunityRecord {
  cost_unavailable?: CostUnavailable;
  customer_id: CustomerId4;
  customer_name?: CustomerName3;
  deduction_treatment?: DeductionTreatment;
  evidence_status: EvidenceStatus1;
  expected_contribution_change_cents: ExpectedContributionChangeCents;
  expected_pocket_gross_profit_change_cents?: ExpectedPocketGrossProfitChangeCents;
  explanation: Explanation1;
  gross_price_opportunity_cents: GrossPriceOpportunityCents;
  gross_price_opportunity_conservative_cents?: GrossPriceOpportunityConservativeCents;
  item_id: ItemId2;
  item_name?: ItemName2;
  pct_below_target?: PctBelowTarget;
  segment: Segment2;
  summary: Summary;
  volume_treatment: VolumeTreatment1;
}
export interface OptionalModules {
  cross_price: CrossPrice;
  cross_price_absent_reason: CrossPriceAbsentReason;
  inventory: InventorySection | null;
  inventory_absent_reason: InventoryAbsentReason;
  win_curves: WinCurves;
  win_curves_absent_reason: WinCurvesAbsentReason;
}
export interface CrossPriceEstimate {
  cross_elasticity: CrossElasticity;
  cross_se: CrossSe;
  group_id: GroupId;
  item_a: ItemA;
  item_b: ItemB;
  n_obs: NObs;
  reasons: Reasons;
  status: Status3;
}
export interface InventorySection {
  cogs_total_cents: CogsTotalCents;
  method: Method;
  n_flagged: NFlagged;
  n_sales_costed: NSalesCosted;
  timing_statement: TimingStatement;
}
export interface WinCurve {
  ci_high: CiHigh;
  ci_low: CiLow;
  n_quotes: NQuotes;
  negotiation_gap: NegotiationGap;
  price_coef: PriceCoef;
  price_coef_se: PriceCoefSe;
  reasons: Reasons1;
  segment: Segment3;
  status: Status4;
  win_rate: WinRate;
}
export interface ReconciliationCheck {
  actual_cents: ActualCents;
  allocated_cents?: AllocatedCents;
  check: Check;
  expected_cents: ExpectedCents;
  passed: Passed;
  residual_cents?: ResidualCents;
  source_total_cents?: SourceTotalCents;
}
export interface Residual {
  amount_cents: AmountCents1;
  name: Name;
  what: What;
  where_listed: WhereListed;
  why_not_allocated: WhyNotAllocated;
}
export interface ScenarioGrid {
  cogs_timing: CogsTiming;
  n_pairs: NPairs;
  name: Name1;
  pct_increase: PctIncrease;
  points: Points;
  scope: Scope1;
  scope_label?: ScopeLabel;
  scope_value: ScopeValue;
}
export interface ScenarioGridPoint {
  contribution_effect_cents: ContributionEffectCents;
  contribution_effect_supported_everywhere: ContributionEffectSupportedEverywhere;
  incremental_cost_cents: IncrementalCostCents1;
  pass_through: PassThrough;
  protected_exposure_cents: ProtectedExposureCents;
  recovered_cents: RecoveredCents;
  residual_exposure_cents: ResidualExposureCents;
}
export interface SegmentsSection {
  bootstrap_median_ari: BootstrapMedianAri;
  index_distribution: IndexDistribution;
  margin_driver_note: MarginDriverNote;
  margin_driver_rules: MarginDriverRules;
  recommendation_change_share: RecommendationChangeShare;
  rules: Rules;
}
export interface SegmentIndexStats {
  n_pairs: NPairs1;
  p25: P25;
  p50: P50;
  p75: P75;
  segment: Segment4;
}
export interface SegmentRule {
  conditions: Conditions;
  display_rank: DisplayRank;
  leaf_id: LeafId;
  mean_target: MeanTarget;
  meaning?: Meaning;
  n_customers: NCustomers1;
  n_items: NItems1;
  n_lines: NLines;
  name: Name2;
  plain: Plain;
  technical_note: TechnicalNote;
}
export interface SensitivitySegment {
  ar_bounded?: ArBounded;
  ar_ci_high: ArCiHigh;
  ar_ci_low: ArCiLow;
  carried_forward_share?: CarriedForwardShare;
  causal_gates?: CausalGates;
  competitor_path_detail?: CompetitorPathDetail;
  competitor_path_resolution?: CompetitorPathResolution;
  estimand?: Estimand;
  estimand_bias_direction?: EstimandBiasDirection;
  first_stage_f: FirstStageF;
  first_stage_f_uncontrolled?: FirstStageFUncontrolled;
  identifying_coverage: IdentifyingCoverage;
  iv: Iv;
  iv_ci_high: IvCiHigh;
  iv_ci_low: IvCiLow;
  iv_se: IvSe;
  market_control_coverage?: MarketControlCoverage;
  market_control_detail?: MarketControlDetail;
  market_control_instrument_r2?: MarketControlInstrumentR2;
  market_control_lag_periods?: MarketControlLagPeriods;
  market_control_price_r2?: MarketControlPriceR2;
  market_control_status?: MarketControlStatus;
  market_control_used?: MarketControlUsed;
  market_control_within_cell_values?: MarketControlWithinCellValues;
  n_identified_instruments?: NIdentifiedInstruments;
  n_obs: NObs1;
  n_pairs: NPairs2;
  n_positive: NPositive;
  naive_fe_ols: NaiveFeOls;
  naive_se: NaiveSe;
  naive_vs_iv_note: NaiveVsIvNote;
  overid_j: OveridJ;
  overid_p: OveridP;
  ppml: Ppml;
  ppml_positive?: PpmlPositive;
  price_cv_within: PriceCvWithin;
  robustness_excluded_weak?: RobustnessExcludedWeak;
  robustness_n_specifications?: RobustnessNSpecifications;
  robustness_spread: RobustnessSpread;
  segment: Segment5;
  status: Status5;
  status_reasons: StatusReasons;
  support_log_hi?: SupportLogHi;
  support_log_lo?: SupportLogLo;
}
/**
 * One causal criterion's outcome (Run 6, Phase 70). Machine-readable so
 * the UI renders a checklist instead of parsing prose.
 */
export interface CausalGate {
  detail: Detail;
  gate: Gate;
  order: Order;
  result: Result;
}
export interface UnallocatedSettlement {
  amount_cents: AmountCents2;
  customer_id: CustomerId5;
  period: Period;
  rebate_program_id: RebateProgramId;
}
export interface ValidationResult {
  achieved: Achieved;
  check: Check1;
  passed: Passed1;
  target: Target;
}
export interface GroupWaterfall {
  key: Key;
  label: Label1;
  net_revenue_cents: NetRevenueCents3;
  stages: Stages;
}
export interface WaterfallStage {
  amount_cents: AmountCents3;
  coverage: Coverage;
  pct_of_list?: PctOfList;
  stage: Stage;
}
