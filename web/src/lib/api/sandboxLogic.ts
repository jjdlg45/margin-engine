/** Demo-sandbox review logic (polish round): the same lifecycle, role and
 * compliance guardrails the API enforces (margin_engine/actions/review.py and
 * model.py), mirrored in TypeScript for the static demo. The sandbox tracks
 * WORKFLOW STATE only - statuses, events, edits, proposals; every business
 * number still comes from the engine's exports. Pure functions here, storage
 * in sandboxAdapter.ts, so the guardrails are unit-testable. */

import type { ActionRow } from "../../types/bundle";
import { ApiError, type ActionUser } from "./types";

/** Named sandbox identities. Priya holds both roles so the separation-of-
 * duties guardrail is demonstrable: she can propose AND approve, but never
 * approve her own proposal. */
export const SANDBOX_USERS: ActionUser[] = [
  { name: "Priya Raman", roles: ["analyst", "approver"] },
  { name: "Dan Okafor", roles: ["analyst"] },
  { name: "Maya Lindqvist", roles: ["approver"] },
];

export const ALLOWED_TRANSITIONS: Record<string, string[]> = {
  proposed: ["under_review"],
  under_review: ["approved", "rejected", "proposed"],
  rejected: ["proposed"],
  approved: ["exported"],
  exported: ["implemented"],
  implemented: ["measured"],
  measured: [],
};

/** Which role may PERFORM a transition to this status (review.py). */
export const TRANSITION_ROLES: Record<string, string[]> = {
  under_review: ["analyst"],
  approved: ["approver"],
  rejected: ["approver"],
  proposed: ["analyst"], // rework after rejection
  exported: ["analyst"],
  implemented: ["analyst"],
  measured: [], // results tracker only, never a user
};

export const KIND_TO_STATUS: Record<string, string> = {
  submit: "under_review",
  approve: "approved",
  reject: "rejected",
  rework: "proposed",
  implement: "implemented",
};

export interface SandboxEvent {
  action_id: string;
  who: string;
  at: string; // ISO datetime
  from_status: string | null;
  to_status: string;
  note: string;
}

export interface SandboxEdit {
  action_id: string;
  who: string;
  at: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
}

export interface SandboxState {
  version: 1;
  events: SandboxEvent[];
  edits: SandboxEdit[];
  proposals: ActionRow[];
}

export const EMPTY_STATE: SandboxState = { version: 1, events: [], edits: [], proposals: [] };

export const EDITABLE_FIELDS = [
  "owner",
  "due_date",
  "title",
  "rationale",
  "proposed_price_cents",
];
export const EDITABLE_STATUSES = ["proposed", "under_review", "rejected"];

const rolesOf = (user: string): string[] => {
  const u = SANDBOX_USERS.find((x) => x.name === user);
  if (!u) {
    throw new ApiError(
      `Unknown user "${user}": choose a working identity from the sandbox roster.`,
      400,
      "unknown_user",
    );
  }
  return u.roles;
};

/** Derived status: the base row's demo status, then every sandbox event. */
export function sandboxStatus(base: ActionRow, state: SandboxState): string {
  let status = base.status;
  for (const e of state.events) {
    if (e.action_id === base.action_id) status = e.to_status;
  }
  return status;
}

/** The base row with sandbox edits applied (latest wins) and derived status/
 * overdue. Overdue keeps the engine's rule: it never flips from a sandbox
 * edit, except that leaving an actionable status clears it. */
export function overlayRow(base: ActionRow, state: SandboxState): ActionRow {
  const row: ActionRow = { ...base };
  for (const e of state.edits) {
    if (e.action_id !== base.action_id) continue;
    (row as unknown as Record<string, unknown>)[e.field] =
      e.field === "proposed_price_cents"
        ? e.new_value === null
          ? null
          : Number(e.new_value)
        : e.new_value;
  }
  row.status = sandboxStatus(base, state);
  if (!["proposed", "under_review", "approved", "exported"].includes(row.status)) {
    row.overdue = false;
  }
  return row;
}

/** Throws ApiError with the same machine-readable codes the API uses when a
 * transition violates policy; returns the target status otherwise. */
export function checkTransition(
  row: ActionRow,
  status: string,
  state: SandboxState,
  kind: string,
  user: string,
  note: string,
): string {
  const to = KIND_TO_STATUS[kind];
  if (!to) throw new ApiError(`Unknown transition "${kind}".`, 400, "unknown_transition");
  const userRoles = rolesOf(user);
  const allowed = TRANSITION_ROLES[to] ?? [];
  if (!allowed.some((r) => userRoles.includes(r))) {
    throw new ApiError(
      `${user} (${userRoles.join(" and ")}) may not move an action to "${to}" - that needs the ${allowed.join(" or ")} role.`,
      403,
      "role_denied",
    );
  }
  if (row.blocked) {
    throw new ApiError(`This action is blocked: ${row.blocked_reason ?? ""}`, 409, "blocked");
  }
  if (!(ALLOWED_TRANSITIONS[status] ?? []).includes(to)) {
    throw new ApiError(
      `An action in status "${status}" cannot move to "${to}".`,
      409,
      "invalid_transition",
    );
  }
  if (to === "approved") {
    // Separation of duties: whoever proposed or submitted it cannot approve.
    const owners = new Set(
      [...row.events, ...state.events.filter((e) => e.action_id === row.action_id)]
        .filter((e) => ["proposed", "under_review"].includes(e.to_status) && e.who !== "engine")
        .map((e) => e.who),
    );
    if (owners.has(user)) {
      throw new ApiError(
        `Separation of duties: ${user} proposed or submitted this action and cannot approve it.`,
        403,
        "self_approval",
      );
    }
    if (row.compliance_flags && !note.trim()) {
      throw new ApiError(
        "This action carries a compliance flag: approval requires a written justification.",
        422,
        "justification_required",
      );
    }
  }
  return to;
}

/** Edit guardrails (review.py check_edit): whitelist, pre-approval statuses,
 * blocked untouched, and a price edit never below the binding floor. */
export function checkEdit(row: ActionRow, status: string, field: string, value: unknown): void {
  if (!EDITABLE_FIELDS.includes(field)) {
    throw new ApiError(`The field "${field}" is not editable.`, 422, "field_not_editable");
  }
  if (!EDITABLE_STATUSES.includes(status)) {
    throw new ApiError(
      `An action in status "${status}" can no longer be edited.`,
      409,
      "status_not_editable",
    );
  }
  if (row.blocked) {
    throw new ApiError(`This action is blocked: ${row.blocked_reason ?? ""}`, 409, "blocked");
  }
  if (field === "proposed_price_cents") {
    if (typeof value !== "number" || !Number.isInteger(value)) {
      throw new ApiError("A proposed price must be whole cents.", 422, "bad_value");
    }
    if (row.floor_price_cents === null || row.floor_price_cents === undefined) {
      throw new ApiError(
        "No binding floor is on record for this pair, so a price edit cannot be guardrail-checked.",
        422,
        "floor_unknown",
      );
    }
    if (value < row.floor_price_cents) {
      throw new ApiError(
        `The proposed price is below the binding floor of ${(row.floor_price_cents / 100).toFixed(2)} dollars; the floor exists to guarantee the margin.`,
        422,
        "below_floor",
      );
    }
  }
}

// ---- Price export (mirrors actions/export.py with the default template) ----

export const PRICE_ACTION_TYPES = ["price_change", "below_floor_fix"];
const HEADERS = [
  "ACTION_ID",
  "APPROVAL_ID",
  "CUSTOMER",
  "ITEM",
  "UOM",
  "EFFECTIVE_DATE",
  "PREVIOUS_PRICE",
  "NEW_PRICE",
];

const fmtPrice = (cents: number): string => {
  const sign = cents < 0 ? "-" : "";
  const mag = Math.abs(cents);
  return `${sign}${Math.floor(mag / 100)}.${String(mag % 100).padStart(2, "0")}`;
};

async function sha256hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The approval id is derived from the approving event exactly as the CLI
 * export derives it: sha256(action|seq|approver|timestamp), prefixed apr_. */
export async function approvalId(
  actionId: string,
  seq: number,
  who: string,
  at: string,
): Promise<string> {
  return `apr_${(await sha256hex(`${actionId}|${seq}|${who}|${at}`)).slice(0, 10)}`;
}

export interface ExportableRow {
  row: ActionRow;
  status: string;
  approval: { seq: number; who: string; at: string } | null;
}

/** CSV text in the CLI's default template: same columns, same header names,
 * same price and date formats, sorted by action id. */
export async function buildPriceCsv(rows: ExportableRow[]): Promise<string> {
  const lines = [HEADERS.join(",")];
  for (const r of [...rows].sort((a, b) => a.row.action_id.localeCompare(b.row.action_id))) {
    const apr = r.approval
      ? await approvalId(r.row.action_id, r.approval.seq, r.approval.who, r.approval.at)
      : "";
    lines.push(
      [
        r.row.action_id,
        apr,
        r.row.customer_id ?? "",
        r.row.item_id ?? "",
        r.row.uom ?? "",
        r.row.due_date ?? "",
        r.row.current_price_cents !== null && r.row.current_price_cents !== undefined
          ? fmtPrice(r.row.current_price_cents)
          : "",
        fmtPrice(r.row.proposed_price_cents ?? 0),
      ].join(","),
    );
  }
  return lines.join("\n") + "\n";
}
