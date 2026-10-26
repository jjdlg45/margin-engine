/** Data-client contract (mirrors docs/api/openapi.yaml). The static adapter
 * implements it over exported JSON files; a live deployment swaps in an HTTP
 * adapter against the BFF - a one-file change in client.ts. */

import type {
  ActionEventRow,
  ActionOutcome,
  ActionRow,
  ActionsSection,
  ActionsSummary,
  CustomerEconomics,
  DispersionSection,
  GroupWaterfall,
  GuidancePair,
  LabelEntry,
  LeakageComponent,
  LibraryWarning,
  ManifestInfo,
  OpportunityRecord,
  OptionalModules,
  OrderEconomicsSummary,
  ReconciliationCheck,
  Residual,
  ScenarioGrid,
  SegmentsSection,
  SensitivitySegment,
  SummaryKpis,
  UnallocatedSettlement,
  ValidationResult,
  WaterfallStage,
} from "../../types/bundle";

export interface RefreshSectionStatus {
  refreshed_utc: string;
  stale: boolean;
  detail?: string;
  data_as_of?: string;
}

export interface AppData {
  bundle_version: string;
  engine_version: string;
  data_label: string;
  honest_labels: string[];
  labels: Record<string, LabelEntry>;
  kpis: SummaryKpis;
  /** Live mode only (30.4): the latest replayed data day. */
  data_as_of?: string;
  /** Live mode only (30.4): per-section refresh status; stale sections are
   * labeled in the shell. */
  refresh_status?: Record<string, RefreshSectionStatus>;
  /** Live mode only (Phase 43): plain-English drift flags vs the previous
   * refresh; empty means a comparison ran and found nothing. */
  drift_flags?: { code: string; message: string }[];
}

export interface SegmentName {
  leaf_id: number;
  display_rank: number;
  name: string;
}

export interface EntityFacet {
  id: string;
  name: string | null;
}

export interface OverviewData {
  waterfall_overall: WaterfallStage[];
  leakage_total_cents: number;
  leakage_top_component: LeakageComponent | null;
  gross_total_cents: number;
  window_label: string;
  below_floor_pairs: number;
  causal_segments: number;
  total_segments: number;
  negative_contribution_orders: number;
  n_library_warnings: number;
  open_actions: ActionsSummary;
}

export interface WaterfallData {
  overall: WaterfallStage[];
  by_segment: GroupWaterfall[];
  by_customer: GroupWaterfall[];
  leakage_components: LeakageComponent[];
  leakage_total_cents: number;
  window_label: string;
  segment_names: SegmentName[];
}

export interface CustomersData {
  orders: OrderEconomicsSummary;
  facets: { customers: EntityFacet[]; service_flags: string[] };
  waterfall_by_customer: GroupWaterfall[];
}

export interface GuidanceData {
  n_pairs: number;
  facets: {
    customers: EntityFacet[];
    items: EntityFacet[];
    statuses: string[];
    segments: SegmentName[];
  };
}

export interface SensitivityData {
  rows: SensitivitySegment[];
  segment_names: SegmentName[];
}

export interface OpportunityData {
  never_summed_note: string;
  window_label: string;
  leakage_total_cents: number;
  gross_total_cents: number;
  gross_conservative_total_cents: number | null;
  expected_gp_total_cents: number;
  expected_total_cents: number;
  expected_total_note: string;
  optimizer_objective: string;
  n_records: number;
  n_cost_unavailable: number;
  n_outside_price_support: number;
  leakage_by_customer: { customer_id: string; net_revenue_cents: number; total_leakage_cents: number; components: LeakageComponent[] }[];
  facets: {
    customers: EntityFacet[];
    items: EntityFacet[];
    evidence: string[];
    treatments: string[];
  };
}

export interface ScenariosData {
  grids: ScenarioGrid[];
}

export interface MethodologyData {
  data_label: string;
  reconciliation: ReconciliationCheck[];
  residuals: Residual[];
  unallocated_settlements: UnallocatedSettlement[];
  library_warnings: LibraryWarning[];
  validation_results: ValidationResult[];
  manifest: ManifestInfo;
}

export interface PageDataMap {
  overview: OverviewData;
  actions: ActionsSection;
  waterfall: WaterfallData;
  customers: CustomersData;
  segments: SegmentsSection;
  guidance: GuidanceData;
  sensitivity: SensitivityData;
  opportunity: OpportunityData;
  scenarios: ScenariosData;
  methodology: MethodologyData;
  modules: OptionalModules;
  dispersion: DispersionSection; // reserved; not currently routed
}

export type PageName = Exclude<keyof PageDataMap, "dispersion">;

export interface TableRowMap {
  guidance: Omit<GuidancePair, "explanation">;
  opportunities: OpportunityRecord;
  customers: CustomerEconomics;
  actions: ActionRow;
}
export type TableName = keyof TableRowMap;

export interface TableQuery {
  page?: number;
  page_size?: number;
  /** "field" ascending or "-field" descending. */
  sort?: string;
  filters?: Record<string, unknown>;
}

export interface TablePage<T> {
  items: T[];
  total: number;
  total_unfiltered: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

/** Transport failure with an HTTP-like status; 4xx must not be retried.
 * `code` carries the API's machine-readable policy reason when present
 * (role_denied, self_approval, justification_required, below_floor, ...). */
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string,
  ) {
    super(message);
  }
}

// ---- Action center (Run 5, Phases 39-42) ----------------------------------

export interface ActionUser {
  name: string;
  roles: string[];
}

export interface ActionDetail {
  action: ActionRow;
  status: string;
  events: ActionEventRow[];
  edits: Record<string, unknown>[];
  outcome: ActionOutcome | null;
}

export interface ActionOutcomeListItem {
  action_id: string;
  title: string;
  customer_id: string | null;
  item_id: string | null;
  outcome: ActionOutcome;
}

export interface TransitionResult {
  action_id: string;
  status: string;
  changed: boolean;
}

export interface BulkApproveItem {
  action_id: string;
  outcome: "approved" | "unchanged" | "refused";
  reason: string;
}

export interface PriceExportResult {
  summary: Record<string, unknown>;
  csv: string;
  n_transitioned: number;
  dry_run: boolean;
}

export type TransitionKind = "submit" | "approve" | "reject" | "rework" | "implement";

/** Review-workflow operations. The static demo is READ-ONLY: readOnly is true
 * and every mutation rejects with a 405 ApiError; the HTTP adapter talks to
 * the live API with X-User (configured local identity, not authentication). */
export interface ActionsClient {
  readOnly: boolean;
  getUsers(): Promise<ActionUser[]>;
  getDetail(actionId: string): Promise<ActionDetail>;
  /** One row per tracked action: the measured outcome or the stated reason it
   * cannot be measured. Static mode derives it from the embedded table rows. */
  getOutcomes(): Promise<ActionOutcomeListItem[]>;
  transition(
    actionId: string,
    kind: TransitionKind,
    user: string,
    note?: string,
  ): Promise<TransitionResult>;
  bulkApprove(actionIds: string[], user: string, reason?: string): Promise<BulkApproveItem[]>;
  edit(
    actionId: string,
    user: string,
    patch: Record<string, unknown>,
  ): Promise<{ action_id: string; changed_fields: string[] }>;
  propose(
    user: string,
    body: Record<string, unknown>,
  ): Promise<{ action_id: string; status: string }>;
  priceExport(user: string, dryRun: boolean): Promise<PriceExportResult>;
}

export interface DataClient {
  getApp(): Promise<AppData>;
  actions: ActionsClient;
  getPage<P extends PageName>(page: P): Promise<PageDataMap[P]>;
  queryTable<T extends TableName>(table: T, query: TableQuery): Promise<TablePage<TableRowMap[T]>>;
  getGuidanceExplanation(customerId: string, itemId: string): Promise<string>;
}
