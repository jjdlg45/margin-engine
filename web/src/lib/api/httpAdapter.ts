/** HTTP adapter: the same DataClient contract served by the BFF
 * (docs/api/openapi.yaml, implemented in api/ in Run 4 Phase 30). Filtering,
 * sorting and pagination happen server-side; the browser never recalculates
 * financials. Selected by VITE_DATA_SOURCE=http (see client.ts); the API base
 * URL comes from VITE_API_BASE_URL (default /api, a same-origin proxy). */

import {
  ActionDetail,
  ActionsClient,
  ActionUser,
  ApiError,
  AppData,
  BulkApproveItem,
  DataClient,
  PageDataMap,
  PageName,
  PriceExportResult,
  TableName,
  TablePage,
  TableQuery,
  TableRowMap,
  TransitionKind,
  TransitionResult,
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

async function sendJson<T>(
  method: "POST" | "PATCH",
  path: string,
  user: string,
  body: Record<string, unknown>,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-User": user,
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(`network failure calling ${path}`, 0);
  }
  const payload = (await res.json().catch(() => ({}))) as {
    detail?: string;
    code?: string;
  };
  if (!res.ok) {
    throw new ApiError(payload.detail ?? `${path}: HTTP ${res.status}`, res.status, payload.code);
  }
  return payload as T;
}

const TRANSITION_PATH: Record<TransitionKind, string> = {
  submit: "submit",
  approve: "approve",
  reject: "reject",
  rework: "rework",
  implement: "implement",
};

const httpActions: ActionsClient = {
  readOnly: false,
  getUsers: async () => (await getJson<{ users: ActionUser[] }>("/actions/users")).users,
  getDetail: (actionId) => getJson<ActionDetail>(`/actions/${encodeURIComponent(actionId)}`),
  async getOutcomes() {
    const [outcomes, queue] = await Promise.all([
      getJson<{ items: Record<string, unknown>[] }>("/actions/outcomes"),
      getJson<{ items: { action_id: string; title: string }[] }>(
        "/actions?page=1&page_size=5000",
      ),
    ]);
    const titles = new Map(queue.items.map((r) => [r.action_id, r.title]));
    return outcomes.items.map((o) => ({
      action_id: String(o.action_id),
      title: titles.get(String(o.action_id)) ?? String(o.action_id),
      customer_id: (o.customer_id as string | null) ?? null,
      item_id: (o.item_id as string | null) ?? null,
      outcome: o as unknown as import("../../types/bundle").ActionOutcome,
    }));
  },
  transition: (actionId, kind, user, note) =>
    sendJson<TransitionResult>(
      "POST",
      `/actions/${encodeURIComponent(actionId)}/${TRANSITION_PATH[kind]}`,
      user,
      kind === "approve" || kind === "reject" ? { reason: note ?? "" } : { note: note ?? "" },
    ),
  bulkApprove: async (actionIds, user, reason) =>
    (
      await sendJson<{ results: BulkApproveItem[] }>("POST", "/actions/bulk-approve", user, {
        action_ids: actionIds,
        reason: reason ?? "",
      })
    ).results,
  edit: (actionId, user, patch) =>
    sendJson<{ action_id: string; changed_fields: string[] }>(
      "PATCH",
      `/actions/${encodeURIComponent(actionId)}`,
      user,
      patch,
    ),
  propose: (user, body) =>
    sendJson<{ action_id: string; status: string }>("POST", "/actions", user, body),
  priceExport: (user, dryRun) =>
    sendJson<PriceExportResult>("POST", "/actions/price-export", user, { dry_run: dryRun }),
};

export const httpAdapter: DataClient = {
  getApp: () => getJson<AppData>("/app"),

  actions: httpActions,

  getPage: <P extends PageName>(page: P) => getJson<PageDataMap[P]>(`/pages/${page}`),

  queryTable: <T extends TableName>(
    table: T,
    query: TableQuery,
  ): Promise<TablePage<TableRowMap[T]>> =>
    // Actions come from the live action store, not the bundle tables.
    table === "actions"
      ? getJson<TablePage<TableRowMap[T]>>(`/actions${tableQueryString(query)}`)
      : getJson<TablePage<TableRowMap[T]>>(`/tables/${table}${tableQueryString(query)}`),

  async getGuidanceExplanation(customerId: string, itemId: string): Promise<string> {
    const body = await getJson<{ explanation: string }>(
      `/tables/guidance/explanations/${encodeURIComponent(customerId)}/${encodeURIComponent(itemId)}`,
    );
    return body.explanation;
  },
};
