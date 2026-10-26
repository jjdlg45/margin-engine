/** The sandbox mirrors the API's review guardrails (review.py); these tests
 * pin the mirrored logic: roles, lifecycle, separation of duties, compliance
 * justification, floor-checked price edits, and the CLI price-file template. */

import { describe, expect, it } from "vitest";
import type { ActionRow } from "../../types/bundle";
import { ApiError } from "./types";
import {
  EMPTY_STATE,
  approvalId,
  buildPriceCsv,
  checkEdit,
  checkTransition,
  overlayRow,
  sandboxStatus,
  type SandboxState,
} from "./sandboxLogic";

function mkRow(over: Partial<ActionRow> = {}): ActionRow {
  return {
    action_id: "act_000000000001",
    action_type: "price_change",
    title: "Raise price",
    rationale: "test",
    customer_id: "C1",
    customer_name: null,
    item_id: "I1",
    item_name: null,
    scope: "customer_item",
    uom: "EA",
    segment: 1,
    guidance_status: "below_reference",
    evidence_status: "descriptive_elasticity",
    volume_treatment: "fixed_volume_only",
    predicted_volume_ratio: 1,
    impact_basis: "gross_price_opportunity_fixed_volume",
    expected_impact_cents: 10_000,
    current_price_cents: 900,
    proposed_price_cents: 1_200,
    floor_price_cents: 1_000,
    priority: "medium",
    owner: "Dan Okafor (pricing analyst)",
    due_date: "2026-03-01",
    overdue: false,
    compliance_flags: "",
    blocked: false,
    blocked_reason: null,
    status: "under_review",
    events: [
      { seq: 0, who: "engine", at: "2026-01-01T00:00:00Z", from_status: null, to_status: "proposed", note: "" },
      { seq: 1, who: "Dan Okafor", at: "2026-01-02T00:00:00Z", from_status: "proposed", to_status: "under_review", note: "" },
    ],
    outcome: null,
    ...over,
  } as ActionRow;
}

const code = (fn: () => unknown): string => {
  try {
    fn();
  } catch (e) {
    if (e instanceof ApiError) return e.code ?? "";
    throw e;
  }
  return "(no error)";
};

describe("checkTransition", () => {
  it("enforces roles per transition", () => {
    const row = mkRow();
    expect(code(() => checkTransition(row, "under_review", EMPTY_STATE, "approve", "Dan Okafor", "x"))).toBe("role_denied");
    expect(checkTransition(row, "under_review", EMPTY_STATE, "approve", "Maya Lindqvist", "x")).toBe("approved");
  });

  it("rejects transitions the lifecycle forbids", () => {
    const row = mkRow({ status: "approved" });
    expect(code(() => checkTransition(row, "approved", EMPTY_STATE, "approve", "Maya Lindqvist", "x"))).toBe("invalid_transition");
    expect(code(() => checkTransition(row, "measured", EMPTY_STATE, "submit", "Dan Okafor", ""))).toBe("invalid_transition");
  });

  it("never advances a blocked action", () => {
    const row = mkRow({ blocked: true, blocked_reason: "contract-protected" });
    expect(code(() => checkTransition(row, "proposed", EMPTY_STATE, "submit", "Dan Okafor", ""))).toBe("blocked");
  });

  it("blocks self-approval, including through sandbox events", () => {
    // Priya holds both roles; she submitted it, so she cannot approve it.
    const state: SandboxState = {
      version: 1,
      events: [
        { action_id: "act_000000000001", who: "Priya Raman", at: "2026-01-03T00:00:00Z", from_status: "proposed", to_status: "under_review", note: "" },
      ],
      edits: [],
      proposals: [],
    };
    const row = mkRow({ events: [mkRow().events[0]] });
    expect(code(() => checkTransition(row, "under_review", state, "approve", "Priya Raman", "x"))).toBe("self_approval");
    expect(checkTransition(row, "under_review", state, "approve", "Maya Lindqvist", "x")).toBe("approved");
  });

  it("demands a written justification on compliance-flagged approvals", () => {
    const row = mkRow({ compliance_flags: "robinson_patman" });
    expect(code(() => checkTransition(row, "under_review", EMPTY_STATE, "approve", "Maya Lindqvist", "  "))).toBe("justification_required");
    expect(checkTransition(row, "under_review", EMPTY_STATE, "approve", "Maya Lindqvist", "cost-to-serve justified")).toBe("approved");
  });

  it("refuses unknown users", () => {
    expect(code(() => checkTransition(mkRow(), "proposed", EMPTY_STATE, "submit", "Mallory", ""))).toBe("unknown_user");
  });
});

describe("checkEdit", () => {
  it("whitelists fields and pre-approval statuses", () => {
    expect(code(() => checkEdit(mkRow(), "proposed", "expected_impact_cents", 1))).toBe("field_not_editable");
    expect(code(() => checkEdit(mkRow(), "approved", "owner", "x"))).toBe("status_not_editable");
  });

  it("never lets a price edit go below the binding floor", () => {
    expect(code(() => checkEdit(mkRow(), "proposed", "proposed_price_cents", 999))).toBe("below_floor");
    expect(code(() => checkEdit(mkRow({ floor_price_cents: null }), "proposed", "proposed_price_cents", 999))).toBe("floor_unknown");
    expect(code(() => checkEdit(mkRow(), "proposed", "proposed_price_cents", 1_000))).toBe("(no error)");
  });
});

describe("overlay and status replay", () => {
  it("derives status from base plus sandbox events and applies edits", () => {
    const state: SandboxState = {
      version: 1,
      events: [
        { action_id: "act_000000000001", who: "Maya Lindqvist", at: "2026-01-05T00:00:00Z", from_status: "under_review", to_status: "approved", note: "ok" },
      ],
      edits: [
        { action_id: "act_000000000001", who: "Dan Okafor", at: "2026-01-04T00:00:00Z", field: "owner", old_value: "a", new_value: "Priya Raman (sales operations lead)" },
      ],
      proposals: [],
    };
    const row = mkRow();
    expect(sandboxStatus(row, state)).toBe("approved");
    const over = overlayRow(row, state);
    expect(over.status).toBe("approved");
    expect(over.owner).toBe("Priya Raman (sales operations lead)");
  });
});

describe("price file", () => {
  it("matches the CLI's default template exactly", async () => {
    const row = mkRow({ status: "approved", proposed_price_cents: 123_456, current_price_cents: 99_999 });
    const csv = await buildPriceCsv([
      { row, status: "approved", approval: { seq: 2, who: "Maya Lindqvist", at: "2026-01-05T00:00:00+00:00" } },
    ]);
    const [header, line] = csv.trimEnd().split("\n");
    expect(header).toBe("ACTION_ID,APPROVAL_ID,CUSTOMER,ITEM,UOM,EFFECTIVE_DATE,PREVIOUS_PRICE,NEW_PRICE");
    const cols = line.split(",");
    expect(cols[0]).toBe("act_000000000001");
    expect(cols[1]).toMatch(/^apr_[0-9a-f]{10}$/);
    expect(cols.slice(2)).toEqual(["C1", "I1", "EA", "2026-03-01", "999.99", "1234.56"]);
    expect(csv.endsWith("\n")).toBe(true);
  });

  it("derives the approval id the way the CLI does", async () => {
    const a = await approvalId("act_1", 2, "Maya", "2026-01-05T00:00:00+00:00");
    const b = await approvalId("act_1", 2, "Maya", "2026-01-05T00:00:00+00:00");
    expect(a).toBe(b);
    expect(a).toMatch(/^apr_[0-9a-f]{10}$/);
  });
});
