/** Demo sandbox (polish round): in the static demo every workflow button
 * works - propose, edit, approve, reject, bulk approve, export, mark
 * implemented - with the same guardrails the API enforces (sandboxLogic.ts).
 * Workflow state lives ONLY in the visitor's browser (localStorage, with an
 * in-memory fallback when storage is unavailable); business numbers still
 * come exclusively from the engine's exported files. */

import type { ActionRow } from "../../types/bundle";
import type { ActionEventRow } from "../../types/bundle";
import {
  ApiError,
  type ActionsClient,
  type ActionDetail,
  type BulkApproveItem,
  type DataClient,
  type PriceExportResult,
  type TableName,
  type TablePage,
  type TableQuery,
  type TableRowMap,
  type TransitionResult,
} from "./types";
import { FILTERS, applySort, staticAdapter, type Row } from "./staticAdapter";
import {
  EMPTY_STATE,
  PRICE_ACTION_TYPES,
  SANDBOX_USERS,
  buildPriceCsv,
  checkEdit,
  checkTransition,
  overlayRow,
  sandboxStatus,
  type ExportableRow,
  type SandboxEvent,
  type SandboxState,
} from "./sandboxLogic";

const KEY = "margin-sandbox-v1";

// Every mutation is read-modify-write over the stored state, so mutations run
// strictly one after another: a click that lands while the previous save is
// still in flight must see its result, never overwrite it.
let mutationChain: Promise<unknown> = Promise.resolve();
function queued<T>(fn: () => Promise<T>): Promise<T> {
  const p = mutationChain.then(fn);
  mutationChain = p.catch(() => undefined);
  return p;
}

// In-memory fallback: a private window or blocked storage still gets a
// working sandbox for the session; it simply forgets on reload.
let memoryState: SandboxState = structuredClone(EMPTY_STATE);

function loadState(): SandboxState {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const s = JSON.parse(raw) as SandboxState;
      if (s && s.version === 1) return s;
    }
  } catch {
    /* storage unavailable: use the in-memory state */
  }
  return memoryState;
}

function saveState(s: SandboxState): void {
  memoryState = s;
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* storage unavailable: the in-memory state still carries the session */
  }
}

/** Clears every sandbox change (the Reset demo button). Queued behind any
 * in-flight mutation so a save can never resurrect cleared state. */
export function resetSandbox(): Promise<void> {
  return queued(async () => {
    memoryState = structuredClone(EMPTY_STATE);
    try {
      localStorage.removeItem(KEY);
    } catch {
      /* nothing stored */
    }
  });
}

async function baseRows(): Promise<ActionRow[]> {
  const page = await staticAdapter.queryTable("actions", { page: 1, page_size: 5000 });
  return page.items as ActionRow[];
}

async function findBase(actionId: string, state: SandboxState): Promise<ActionRow> {
  const proposal = state.proposals.find((p) => p.action_id === actionId);
  if (proposal) return proposal;
  const row = (await baseRows()).find((r) => r.action_id === actionId);
  if (!row) throw new ApiError(`unknown action ${actionId}`, 404);
  return row;
}

function combinedEvents(base: ActionRow, state: SandboxState): ActionEventRow[] {
  const extra = state.events
    .filter((e) => e.action_id === base.action_id)
    .map((e, i) => ({
      seq: base.events.length + i,
      who: e.who,
      at: e.at,
      from_status: e.from_status,
      to_status: e.to_status,
      note: e.note,
    }));
  return [...base.events, ...extra];
}

function requireRole(user: string, role: string): void {
  const u = SANDBOX_USERS.find((x) => x.name === user);
  if (!u) {
    throw new ApiError(
      `Unknown user "${user}": choose a working identity from the sandbox roster.`,
      400,
      "unknown_user",
    );
  }
  if (!u.roles.includes(role)) {
    throw new ApiError(`${user} lacks the ${role} role.`, 403, "role_denied");
  }
}

function appendEvent(
  state: SandboxState,
  actionId: string,
  user: string,
  fromStatus: string,
  toStatus: string,
  note: string,
): SandboxState {
  const ev: SandboxEvent = {
    action_id: actionId,
    who: user,
    at: new Date().toISOString(),
    from_status: fromStatus,
    to_status: toStatus,
    note,
  };
  return { ...state, events: [...state.events, ev] };
}

const sandboxActions: ActionsClient = {
  readOnly: false,

  getUsers: () => Promise.resolve(SANDBOX_USERS),

  async getDetail(actionId: string): Promise<ActionDetail> {
    const state = loadState();
    const base = await findBase(actionId, state);
    const row = overlayRow(base, state);
    return {
      action: { ...row, events: combinedEvents(base, state) },
      status: row.status,
      events: combinedEvents(base, state),
      edits: state.edits
        .filter((e) => e.action_id === actionId)
        .map((e) => ({
          field: e.field,
          old_value: e.old_value,
          new_value: e.new_value,
          who: e.who,
        })),
      outcome: base.outcome ?? null,
    };
  },

  getOutcomes: () => staticAdapter.actions.getOutcomes(),

  transition(actionId, kind, user, note = ""): Promise<TransitionResult> {
    return queued(async () => {
      const state = loadState();
      const base = await findBase(actionId, state);
      const status = sandboxStatus(base, state);
      const to = checkTransition(overlayRow(base, state), status, state, kind, user, note);
      saveState(appendEvent(state, actionId, user, status, to, note));
      return { action_id: actionId, status: to, changed: true };
    });
  },

  bulkApprove(actionIds, user, reason = ""): Promise<BulkApproveItem[]> {
    return queued(async () => {
    let state = loadState();
    const out: BulkApproveItem[] = [];
    for (const id of actionIds) {
      try {
        const base = await findBase(id, state);
        const status = sandboxStatus(base, state);
        if (status === "approved") {
          out.push({ action_id: id, outcome: "unchanged", reason: "already approved" });
          continue;
        }
        checkTransition(overlayRow(base, state), status, state, "approve", user, reason);
        state = appendEvent(state, id, user, status, "approved", reason);
        out.push({ action_id: id, outcome: "approved", reason: "" });
      } catch (e) {
        out.push({
          action_id: id,
          outcome: "refused",
          reason: e instanceof Error ? e.message : "refused",
        });
      }
    }
    saveState(state);
    return out;
    });
  },

  edit(actionId, user, patch) {
    return queued(async () => {
    requireRole(user, "analyst");
    let state = loadState();
    const base = await findBase(actionId, state);
    const status = sandboxStatus(base, state);
    const current = overlayRow(base, state);
    const changed: string[] = [];
    for (const [field, value] of Object.entries(patch)) {
      checkEdit(current, status, field, value);
      const old = (current as unknown as Record<string, unknown>)[field];
      state = {
        ...state,
        edits: [
          ...state.edits,
          {
            action_id: actionId,
            who: user,
            at: new Date().toISOString(),
            field,
            old_value: old === null || old === undefined ? null : String(old),
            new_value: value === null || value === undefined ? null : String(value),
          },
        ],
      };
      changed.push(field);
    }
    saveState(state);
    return { action_id: actionId, changed_fields: changed };
    });
  },

  propose(user, body) {
    return queued(async () => {
    requireRole(user, "analyst");
    const actionType = String(body.action_type ?? "");
    const title = String(body.title ?? "").trim();
    const rationale = String(body.rationale ?? "").trim();
    if (!title || !rationale) {
      throw new ApiError("A title and a rationale are required.", 422, "bad_request");
    }
    if (PRICE_ACTION_TYPES.includes(actionType)) {
      throw new ApiError(
        "Price changes are proposed from the guidance page so the floor can be checked.",
        422,
        "floor_unknown",
      );
    }
    const state = loadState();
    const now = new Date().toISOString();
    const actionId = `act_sbx${(Date.now() % 0xffffffff).toString(16)}${state.proposals.length}`;
    const row: ActionRow = {
      action_id: actionId,
      action_type: actionType || "service_policy",
      title,
      rationale,
      customer_id: (body.customer_id as string | undefined) ?? null,
      customer_name: null,
      item_id: (body.item_id as string | undefined) ?? null,
      item_name: null,
      scope: "manual",
      uom: null,
      segment: null,
      guidance_status: null,
      evidence_status: "manual",
      volume_treatment: null,
      predicted_volume_ratio: null,
      impact_basis: "manual_proposal",
      expected_impact_cents: null,
      current_price_cents: null,
      proposed_price_cents: null,
      floor_price_cents: null,
      priority: "medium",
      owner: user,
      due_date: null,
      overdue: false,
      compliance_flags: "",
      blocked: false,
      blocked_reason: null,
      status: "proposed",
      events: [
        {
          seq: 0,
          who: user,
          at: now,
          from_status: null,
          to_status: "proposed",
          note: "manually proposed (sandbox)",
        },
      ],
      outcome: null,
    };
    saveState({ ...state, proposals: [...state.proposals, row] });
    return { action_id: actionId, status: "proposed" };
    });
  },

  priceExport(user, dryRun): Promise<PriceExportResult> {
    return queued(async () => {
    requireRole(user, "analyst");
    let state = loadState();
    const rows = [...(await baseRows()), ...state.proposals];
    const exportable: ExportableRow[] = [];
    for (const base of rows) {
      const row = overlayRow(base, state);
      if (!PRICE_ACTION_TYPES.includes(row.action_type)) continue;
      if (!["approved", "exported"].includes(row.status)) continue;
      if (row.proposed_price_cents === null || row.proposed_price_cents === undefined) continue;
      const events = combinedEvents(base, state);
      const approvals = events.filter((e) => e.to_status === "approved");
      const last = approvals[approvals.length - 1];
      exportable.push({
        row,
        status: row.status,
        approval: last ? { seq: last.seq, who: last.who, at: last.at } : null,
      });
    }
    const csv = await buildPriceCsv(exportable);
    const byBasis: Record<string, number> = {};
    for (const r of exportable) {
      byBasis[r.row.impact_basis] =
        (byBasis[r.row.impact_basis] ?? 0) + (r.row.expected_impact_cents ?? 0);
    }
    let transitioned = 0;
    if (!dryRun) {
      for (const r of exportable) {
        if (r.status !== "approved") continue;
        state = appendEvent(
          state,
          r.row.action_id,
          user,
          "approved",
          "exported",
          "included in the ERP price export (sandbox)",
        );
        transitioned += 1;
      }
      saveState(state);
    }
    return {
      summary: {
        n_changes: exportable.length,
        expected_impact_by_basis_cents: byBasis,
      },
      csv,
      n_transitioned: transitioned,
      dry_run: dryRun,
    };
    });
  },
};

export const sandboxAdapter: DataClient = {
  getApp: () => staticAdapter.getApp(),

  actions: sandboxActions,

  async getPage(page) {
    const data = await staticAdapter.getPage(page);
    if (page !== "actions") return data;
    const state = loadState();
    const rows = [...(await baseRows()), ...state.proposals].map((r) => overlayRow(r, state));
    const byStatus: Record<string, number> = {};
    for (const r of rows) byStatus[r.status] = (byStatus[r.status] ?? 0) + 1;
    const actions = data as import("../../types/bundle").ActionsSection;
    return {
      ...actions,
      read_only: false,
      summary: {
        ...actions.summary,
        n_actions: rows.length,
        by_status: Object.fromEntries(Object.entries(byStatus).sort()),
      },
      facets: {
        ...actions.facets,
        statuses: [...new Set(rows.map((r) => r.status))].sort(),
        owners: [...new Set(rows.map((r) => r.owner))].sort(),
      },
    } as unknown as typeof data;
  },

  async queryTable<T extends TableName>(
    table: T,
    query: TableQuery,
  ): Promise<TablePage<TableRowMap[T]>> {
    if (table !== "actions") return staticAdapter.queryTable(table, query);
    const state = loadState();
    const rows = [...(await baseRows()), ...state.proposals].map(
      (r) => overlayRow(r, state) as unknown as Row,
    );
    const filters = query.filters ?? {};
    const filtered = rows.filter((r) => FILTERS.actions(r, filters));
    const sorted = applySort(filtered, query.sort);
    const page = Math.max(1, query.page ?? 1);
    const pageSize = Math.min(Math.max(1, query.page_size ?? 50), 5000);
    const start = (page - 1) * pageSize;
    const slice = sorted.slice(start, start + pageSize);
    return {
      items: slice as TableRowMap[T][],
      total: filtered.length,
      total_unfiltered: rows.length,
      page,
      page_size: pageSize,
      has_more: start + slice.length < filtered.length,
    };
  },

  getGuidanceExplanation: (customerId, itemId) =>
    staticAdapter.getGuidanceExplanation(customerId, itemId),
};
