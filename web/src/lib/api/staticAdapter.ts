/** Static-file adapter: implements the BFF contract (docs/api/openapi.yaml)
 * over the exported JSON files. Pagination, sorting and named filters run
 * locally as pure selection - no financial computation. Swapping this file
 * for an HTTP adapter is the whole go-live change (see client.ts). */

import {
  ApiError,
  AppData,
  DataClient,
  PageDataMap,
  PageName,
  TableName,
  TablePage,
  TableQuery,
  TableRowMap,
} from "./types";

async function fetchJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${import.meta.env.BASE_URL}data/${path}`);
  } catch {
    throw new ApiError(`network failure fetching ${path}`, 0);
  }
  if (!res.ok) throw new ApiError(`${path}: HTTP ${res.status}`, res.status);
  return (await res.json()) as T;
}

/** Oversized table files are sharded into "next"-linked chunks (a transport
 * detail of static hosting; the HTTP adapter gets paginated responses from
 * the BFF instead). Follows the chain and concatenates items. */
async function fetchTableRows(path: string): Promise<Row[]> {
  const rows: Row[] = [];
  let nextPath: string | null = path;
  while (nextPath) {
    const page: { items: Row[]; next?: string | null } = await fetchJson(nextPath);
    rows.push(...page.items);
    nextPath = page.next ?? null;
  }
  return rows;
}

async function fetchKeyedShards(path: string): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  let nextPath: string | null = path;
  while (nextPath) {
    const page: { items: Record<string, string>; next?: string | null } =
      await fetchJson(nextPath);
    Object.assign(out, page.items);
    nextPath = page.next ?? null;
  }
  return out;
}

type Row = Record<string, unknown>;
type Predicate = (row: Row, filters: Record<string, unknown>) => boolean;

/** Nullable guidance-row fields whose nulls the exported file omits. */
const GUIDANCE_NULLABLE = [
  "segment",
  "customer_name",
  "item_name",
  "current_index",
  "band_p25",
  "band_p50",
  "band_p75",
  "floor_index",
  "pocket_floor_cents",
  "invoice_floor_cents",
  "invoice_floor_gross_cents",
  "invoice_contribution_floor_cents",
  "binding_floor_basis",
  "list_provenance",
  "target_index",
  "target_label",
  "annual_net_revenue_cents",
];

const num = (v: unknown): number | null =>
  typeof v === "number" && isFinite(v) ? v : null;

function range(value: number | null, lo: unknown, hi: unknown): boolean {
  if (value === null) return lo === undefined && hi === undefined;
  if (lo !== undefined && lo !== null && value < Number(lo)) return false;
  if (hi !== undefined && hi !== null && value > Number(hi)) return false;
  return true;
}

const list = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);

/** Named filters per table - the same vocabulary the OpenAPI spec declares. */
const FILTERS: Record<TableName, Predicate> = {
  guidance: (r, f) =>
    (!f.customer || r.customer_id === f.customer) &&
    (!f.item || r.item_id === f.item) &&
    (!list(f.statuses).length || list(f.statuses).includes(String(r.status))) &&
    (!list(f.segments).length || list(f.segments).includes(String(r.segment ?? ""))) &&
    range(num(r.current_index) !== null ? num(r.current_index)! * 100 : null, f.pol_min, f.pol_max) &&
    range(
      num(r.current_index) !== null && num(r.floor_index) !== null
        ? (num(r.current_index)! - num(r.floor_index)!) * 100
        : null,
      f.gap_min,
      f.gap_max,
    ),
  opportunities: (r, f) =>
    (!f.customer || r.customer_id === f.customer) &&
    (!f.item || r.item_id === f.item) &&
    (!list(f.evidence).length ||
      list(f.evidence).includes(String(r.evidence_status ?? "insufficient_data"))) &&
    (!list(f.treatments).length || list(f.treatments).includes(String(r.volume_treatment))) &&
    range(num(r.gross_price_opportunity_cents)! / 100, f.gross_min, f.gross_max) &&
    range(
      num(r.pct_below_target) !== null ? num(r.pct_below_target)! * 100 : null,
      f.below_min,
      f.below_max,
    ),
  customers: (r, f) =>
    (!f.customer || r.customer_id === f.customer) &&
    (!list(f.flags).length ||
      list(f.flags).some((fl) => (r.service_flags as string[]).includes(fl))) &&
    (!f.losing || (r.contribution_after_activity_cents as number) < 0) &&
    range(num(r.returns_contribution_cents)! / 100, f.returns_min, f.returns_max) &&
    range(num(r.pct_revenue_excluded)! * 100, f.excluded_min, f.excluded_max) &&
    range(num(r.n_orders), f.orders_min, f.orders_max),
};

function applySort(rows: Row[], sort?: string): Row[] {
  if (!sort) return rows;
  const desc = sort.startsWith("-");
  const field = desc ? sort.slice(1) : sort;
  const sorted = [...rows].sort((a, b) => {
    const av = a[field];
    const bv = b[field];
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    return av < bv ? -1 : 1;
  });
  return desc ? sorted.reverse() : sorted;
}

export const staticAdapter: DataClient = {
  getApp: () => fetchJson<AppData>("app.json"),

  getPage: <P extends PageName>(page: P) => fetchJson<PageDataMap[P]>(`${page}.json`),

  async queryTable<T extends TableName>(
    table: T,
    query: TableQuery,
  ): Promise<TablePage<TableRowMap[T]>> {
    const items = await fetchTableRows(`tables/${table}.json`);
    // The guidance file omits null fields to stay under the size gate; the
    // API contract has them as nulls, so restore them here (an HTTP adapter
    // receives them from the BFF as-is).
    if (table === "guidance") {
      for (const r of items) {
        for (const k of GUIDANCE_NULLABLE) {
          if (!(k in r)) r[k] = null;
        }
      }
    }
    const filters = query.filters ?? {};
    const filtered = items.filter((r) => FILTERS[table](r, filters));
    const sorted = applySort(filtered, query.sort);
    const page = Math.max(1, query.page ?? 1);
    const pageSize = Math.min(Math.max(1, query.page_size ?? 50), 5000);
    const start = (page - 1) * pageSize;
    const slice = sorted.slice(start, start + pageSize);
    return {
      items: slice as TableRowMap[T][],
      total: filtered.length,
      total_unfiltered: items.length,
      page,
      page_size: pageSize,
      has_more: start + slice.length < filtered.length,
    };
  },

  async getGuidanceExplanation(customerId: string, itemId: string): Promise<string> {
    const items = await fetchKeyedShards("tables/guidance_explanations.json");
    const text = items[`${customerId}|${itemId}`];
    if (!text) throw new ApiError(`no explanation for ${customerId}/${itemId}`, 404);
    return text;
  },
};
