/* Generated from schema.json (margin-engine export-web). Do not edit. */

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
export type CustomerId = string;
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
export type CurrentIndex = number | null;
export type CustomerId1 = string;
export type Explanation = string;
export type FloorIndex = number | null;
export type InvoiceFloorCents = number | null;
export type ItemId = string;
export type PocketFloorCents = number | null;
export type Segment = number | null;
export type Status = string;
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
 * After returns, like every headline KPI - never exceeds net revenue
 */
export type PocketRevenueCents = number;
export type ReconciliationPass = boolean;
/**
 * Linked return lines (negative)
 */
export type ReturnsCents = number;
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
export type Notes = string[];
export type RulesSha256 = string;
export type ExpectedTotalCents = number;
export type ExpectedTotalNote = string;
export type GrossTotalCents = number;
export type Components = LeakageComponent[];
export type CustomerId2 = string;
export type NetRevenueCents2 = number;
export type TotalLeakageCents = number;
export type LeakageByCustomer = CustomerLeakage[];
export type LeakageTotalCents = number;
export type NRecords = number;
export type NeverSummedNote = string;
export type CustomerId3 = string;
export type EvidenceStatus = string | null;
export type ExpectedContributionChangeCents = number | null;
export type Explanation1 = string;
export type GrossPriceOpportunityCents = number;
export type ItemId1 = string;
export type Segment1 = number | null;
/**
 * One short row-specific sentence for the table
 */
export type Summary = string;
export type VolumeTreatment = string;
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
export type Status1 = string;
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
export type Segment2 = number;
export type Status2 = string;
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
export type IncrementalCostCents = number;
export type PassThrough = number;
export type ProtectedExposureCents = number;
export type RecoveredCents = number;
export type ResidualExposureCents = number;
export type Points = ScenarioGridPoint[];
export type Scope = string;
export type ScopeValue = string;
export type ScenarioGrids = ScenarioGrid[];
export type BootstrapMedianAri = number | null;
export type NPairs1 = number;
export type P25 = number;
export type P50 = number;
export type P75 = number;
export type Segment3 = number;
export type IndexDistribution = SegmentIndexStats[];
export type MarginDriverNote = string;
/**
 * 1-based rank by size; viewers see 'Segment {rank}'
 */
export type DisplayRank = number;
/**
 * Tree node id - technical detail only
 */
export type LeafId = number;
export type MeanTarget = number;
export type NCustomers1 = number;
export type NItems1 = number;
export type NLines = number;
/**
 * Short plain-English name derived from the rule
 */
export type Name2 = string;
/**
 * One-sentence description derived from the rule
 */
export type Plain = string;
/**
 * Raw tree rule - technical detail, not primary display
 */
export type Rule = string;
/**
 * Merge/leaf mechanics for the expander
 */
export type TechnicalNote = string;
export type MarginDriverRules = SegmentRule[];
export type RecommendationChangeShare = number | null;
export type Rules = SegmentRule[];
export type ArCiHigh = number | null;
export type ArCiLow = number | null;
export type FirstStageF = number | null;
export type IdentifyingCoverage = number | null;
export type Iv = number | null;
export type IvCiHigh = number | null;
export type IvCiLow = number | null;
export type IvSe = number | null;
export type NObs1 = number;
export type NPairs2 = number;
export type NPositive = number;
export type NaiveFeOls = number | null;
export type NaiveSe = number | null;
export type NaiveVsIvNote = string;
export type OveridJ = number | null;
export type OveridP = number | null;
export type Ppml = number | null;
export type PriceCvWithin = number;
export type RobustnessSpread = number | null;
export type Segment4 = number;
export type Status3 = string;
export type StatusReasons = string;
export type Sensitivity = SensitivitySegment[];
export type AmountCents2 = number;
export type CustomerId4 = string;
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
  customer_id: CustomerId;
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
  current_index: CurrentIndex;
  customer_id: CustomerId1;
  explanation: Explanation;
  floor_index: FloorIndex;
  invoice_floor_cents: InvoiceFloorCents;
  item_id: ItemId;
  pocket_floor_cents: PocketFloorCents;
  segment: Segment;
  status: Status;
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
  net_revenue_cents: NetRevenueCents1;
  pocket_gross_profit_cents: PocketGrossProfitCents;
  pocket_margin_pct?: PocketMarginPct;
  pocket_revenue_cents: PocketRevenueCents;
  reconciliation_pass: ReconciliationPass;
  returns_cents: ReturnsCents;
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
  notes: Notes;
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
  expected_total_cents: ExpectedTotalCents;
  expected_total_note: ExpectedTotalNote;
  gross_total_cents: GrossTotalCents;
  leakage_by_customer: LeakageByCustomer;
  leakage_total_cents: LeakageTotalCents;
  n_records: NRecords;
  never_summed_note: NeverSummedNote;
  top_opportunities: TopOpportunities;
  window_label: WindowLabel1;
}
export interface CustomerLeakage {
  components: Components;
  customer_id: CustomerId2;
  net_revenue_cents: NetRevenueCents2;
  total_leakage_cents: TotalLeakageCents;
}
export interface OpportunityRecord {
  customer_id: CustomerId3;
  evidence_status: EvidenceStatus;
  expected_contribution_change_cents: ExpectedContributionChangeCents;
  explanation: Explanation1;
  gross_price_opportunity_cents: GrossPriceOpportunityCents;
  item_id: ItemId1;
  segment: Segment1;
  summary: Summary;
  volume_treatment: VolumeTreatment;
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
  status: Status1;
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
  segment: Segment2;
  status: Status2;
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
  scope: Scope;
  scope_value: ScopeValue;
}
export interface ScenarioGridPoint {
  contribution_effect_cents: ContributionEffectCents;
  contribution_effect_supported_everywhere: ContributionEffectSupportedEverywhere;
  incremental_cost_cents: IncrementalCostCents;
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
  segment: Segment3;
}
export interface SegmentRule {
  display_rank: DisplayRank;
  leaf_id: LeafId;
  mean_target: MeanTarget;
  n_customers: NCustomers1;
  n_items: NItems1;
  n_lines: NLines;
  name: Name2;
  plain: Plain;
  rule: Rule;
  technical_note: TechnicalNote;
}
export interface SensitivitySegment {
  ar_ci_high: ArCiHigh;
  ar_ci_low: ArCiLow;
  first_stage_f: FirstStageF;
  identifying_coverage: IdentifyingCoverage;
  iv: Iv;
  iv_ci_high: IvCiHigh;
  iv_ci_low: IvCiLow;
  iv_se: IvSe;
  n_obs: NObs1;
  n_pairs: NPairs2;
  n_positive: NPositive;
  naive_fe_ols: NaiveFeOls;
  naive_se: NaiveSe;
  naive_vs_iv_note: NaiveVsIvNote;
  overid_j: OveridJ;
  overid_p: OveridP;
  ppml: Ppml;
  price_cv_within: PriceCvWithin;
  robustness_spread: RobustnessSpread;
  segment: Segment4;
  status: Status3;
  status_reasons: StatusReasons;
}
export interface UnallocatedSettlement {
  amount_cents: AmountCents2;
  customer_id: CustomerId4;
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
