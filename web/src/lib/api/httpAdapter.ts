/** HTTP adapter: the same DataClient contract served by the BFF
 * (docs/api/openapi.yaml, implemented in api/ in Run 4 Phase 30). Filtering,
 * sorting and pagination happen server-side; the browser never recalculates
 * financials. Selected by VITE_DATA_SOURCE=http (see client.ts); the API base
 * URL comes from VITE_API_BASE_URL (default /api, a same-origin proxy). */

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

const BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "/api";

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError(`network failure fetching ${path}`, 0);
  }
  if (!res.ok) throw new ApiError(`${path}: HTTP ${res.status}`, res.status);
  return (await res.json()) as T;
}

/** Build the table query string exactly as the OpenAPI contract declares:
 * page, page_size, sort ("field" / "-field"), filters as URL-encoded JSON. */
export function tableQueryString(query: TableQuery): string {
  const params = new URLSearchParams();
  if (query.page !== undefined) params.set("page", String(query.page));
  if (query.page_size !== undefined) params.set("page_size", String(query.page_size));
  if (query.sort) params.set("sort", query.sort);
  const filters = query.filters ?? {};
  const active = Object.fromEntries(
    Object.entries(filters).filter(([, v]) => v !== undefined && v !== null),
  );
  if (Object.keys(active).length) params.set("filters", JSON.stringify(active));
  const s = params.toString();
  return s ? `?${s}` : "";
}

export const httpAdapter: DataClient = {
  getApp: () => getJson<AppData>("/app"),

  getPage: <P extends PageName>(page: P) => getJson<PageDataMap[P]>(`/pages/${page}`),

  queryTable: <T extends TableName>(
    table: T,
    query: TableQuery,
  ): Promise<TablePage<TableRowMap[T]>> =>
    getJson<TablePage<TableRowMap[T]>>(`/tables/${table}${tableQueryString(query)}`),

  async getGuidanceExplanation(customerId: string, itemId: string): Promise<string> {
    const body = await getJson<{ explanation: string }>(
      `/tables/guidance/explanations/${encodeURIComponent(customerId)}/${encodeURIComponent(itemId)}`,
    );
    return body.explanation;
  },
};
