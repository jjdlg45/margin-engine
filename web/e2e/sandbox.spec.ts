/** Demo sandbox (polish round): in the static demo every workflow button
 * works - propose, edit, approve, reject, bulk approve, export, mark
 * implemented - with the API's guardrails (no self-approval, justification on
 * compliance flags, contracts blocked; the below-floor rule is enforced in
 * sandboxLogic and pinned by its unit tests). State stays in the visitor's
 * browser: it survives a reload and Reset demo clears it. */

import { expect, test } from "@playwright/test";
import fs from "node:fs";
import type { ActionRow, WebBundle } from "../src/types/bundle";

let bundle: WebBundle;
test.beforeAll(async ({ request }) => {
  bundle = (await (await request.get("/data/bundle.json")).json()) as WebBundle;
});

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  // Every test starts from a clean sandbox (and a fresh identity).
  await page.goto("/#/");
  await page.evaluate(() => {
    try {
      localStorage.clear();
    } catch {
      /* fine */
    }
  });
});

const label = (code: string) => bundle.labels[code]?.label ?? code;

const find = (pred: (a: ActionRow) => boolean) => bundle.actions_table.find(pred);

async function workAs(page: import("@playwright/test").Page, user: string) {
  await page.goto("/#/actions");
  await page.getByLabel("Working as").selectOption(user);
}

test("full lifecycle: submit, approve, export, mark implemented", async ({ page }) => {
  const a = find((x) => x.status === "proposed" && !x.blocked && !x.compliance_flags);
  test.skip(!a, "needs an open proposed action");
  // Submit as the analyst.
  await workAs(page, "Dan Okafor");
  await page.goto(`/#/actions/${a!.action_id}`);
  await page.getByTestId("transition-submit").click();
  await expect(page.getByText(label("under_review")).first()).toBeVisible();
  // Approve as the approver (a different person - separation of duties).
  await workAs(page, "Maya Lindqvist");
  await page.goto(`/#/actions/${a!.action_id}`);
  await page.getByTestId("decision-note").fill("fits the reference band");
  await page.getByTestId("transition-approve").click();
  await expect(page.getByText(label("approved")).first()).toBeVisible();
  // Export moves approved price actions to exported and downloads the file.
  if (a!.proposed_price_cents !== null) {
    await workAs(page, "Dan Okafor");
    const downloadP = page.waitForEvent("download");
    await page.getByTestId("export-run").click();
    const download = await downloadP;
    expect(download.suggestedFilename()).toBe("prices.csv");
    await expect(page.getByTestId("export-summary")).toContainText("moved to exported");
    // Mark implemented from the detail page.
    await page.goto(`/#/actions/${a!.action_id}`);
    await expect(page.getByText(label("exported")).first()).toBeVisible();
    await page.getByTestId("transition-implement").click();
    await expect(page.getByText(label("implemented")).first()).toBeVisible();
  }
});

test("reject and rework", async ({ page }) => {
  const a = find((x) => x.status === "under_review" && !x.blocked);
  test.skip(!a, "needs an action under review");
  await workAs(page, "Maya Lindqvist");
  await page.goto(`/#/actions/${a!.action_id}`);
  await page.getByTestId("decision-note").fill("ask for a smaller first step");
  await page.getByTestId("transition-reject").click();
  await expect(page.getByText(label("rejected")).first()).toBeVisible();
  await workAs(page, "Dan Okafor");
  await page.goto(`/#/actions/${a!.action_id}`);
  await page.getByTestId("transition-rework").click();
  await expect(page.getByTestId("transition-submit")).toBeVisible();
});

test("guardrail: the proposer cannot approve their own submission", async ({ page }) => {
  const a = find((x) => x.status === "proposed" && !x.blocked && !x.compliance_flags);
  test.skip(!a, "needs an open proposed action");
  // Priya holds both roles: she submits, then tries to approve.
  await workAs(page, "Priya Raman");
  await page.goto(`/#/actions/${a!.action_id}`);
  await page.getByTestId("transition-submit").click();
  await expect(page.getByText(label("under_review")).first()).toBeVisible();
  await page.getByTestId("decision-note").fill("looks fine to me");
  await page.getByTestId("transition-approve").click();
  await expect(page.getByTestId("workflow-error")).toContainText("Separation of duties");
  // A different approver may approve it.
  await workAs(page, "Maya Lindqvist");
  await page.goto(`/#/actions/${a!.action_id}`);
  await page.getByTestId("decision-note").fill("reviewed and supported");
  await page.getByTestId("transition-approve").click();
  await expect(page.getByText(label("approved")).first()).toBeVisible();
});

test("guardrail: compliance-flagged approval demands a written justification", async ({ page }) => {
  let a = find((x) => x.status === "under_review" && !!x.compliance_flags && !x.blocked);
  if (!a) {
    a = find((x) => x.status === "proposed" && !!x.compliance_flags && !x.blocked);
    test.skip(!a, "needs an open compliance-flagged action");
    await workAs(page, "Dan Okafor");
    await page.goto(`/#/actions/${a!.action_id}`);
    await page.getByTestId("transition-submit").click();
    await expect(page.getByText(label("under_review")).first()).toBeVisible();
  }
  await workAs(page, "Maya Lindqvist");
  await page.goto(`/#/actions/${a!.action_id}`);
  await page.getByTestId("transition-approve").click();
  await expect(page.getByTestId("workflow-error")).toContainText("justification");
  await page.getByTestId("decision-note").fill(
    "Price difference reflects the documented cost-to-serve difference.",
  );
  await page.getByTestId("transition-approve").click();
  await expect(page.getByText(label("approved")).first()).toBeVisible();
});

test("guardrail: blocked (contract-protected) actions offer no transition", async ({ page }) => {
  const a = find((x) => x.blocked);
  test.skip(!a, "needs a blocked action");
  await workAs(page, "Priya Raman");
  await page.goto(`/#/actions/${a!.action_id}`);
  await expect(page.getByText(/Blocked:/).first()).toBeVisible();
  await expect(page.getByTestId("transition-submit")).toHaveCount(0);
  await expect(page.getByTestId("transition-approve")).toHaveCount(0);
});

test("propose and edit: a manual proposal appears and its owner can change", async ({ page }) => {
  await workAs(page, "Priya Raman");
  await page.getByRole("button", { name: "Propose an action" }).click();
  await page.getByTestId("propose-form").getByLabel("Title").fill("Review delivery frequency for C9");
  await page.getByTestId("propose-form").getByLabel("Rationale").fill("Weekly deliveries on tiny orders.");
  await page.getByRole("button", { name: "Submit proposal" }).click();
  await expect(page.getByTestId("propose-form")).toHaveCount(0);
  // One more action than the bundle shipped (retrying assertion: the count
  // re-renders after the refetch).
  const expected = (bundle.actions_table.length + 1).toLocaleString("en-US");
  await expect(page.getByText(new RegExp(`Showing [\\d,]+ of ${expected} actions`))).toBeVisible();
  // Open it directly (a proposal has no stated impact, so it sorts last in
  // the queue) and edit the owner.
  const id = await page.evaluate(() => {
    const s = JSON.parse(localStorage.getItem("margin-sandbox-v1") ?? "{}") as {
      proposals?: { action_id: string }[];
    };
    return s.proposals?.[0]?.action_id ?? "";
  });
  expect(id).toBeTruthy();
  await page.goto(`/#/actions/${id}`);
  await expect(page.getByRole("heading", { name: "Review delivery frequency for C9" })).toBeVisible();
  await page.getByTestId("owner-input").fill("Tom Alvarez (category manager)");
  await page.getByTestId("owner-save").click();
  await expect(page.getByText("Saved.")).toBeVisible();
});

test("bulk approve runs per-item guardrails", async ({ page }) => {
  const underReview = bundle.actions_table.filter((x) => x.status === "under_review" && !x.blocked);
  test.skip(underReview.length === 0, "needs actions under review");
  await workAs(page, "Maya Lindqvist");
  await page.getByTestId("bulk-note").fill("Reviewed in the weekly pricing meeting.");
  await page.getByTestId("bulk-approve").click();
  await expect(page.getByTestId("bulk-result")).toContainText(/Approved \d+ of \d+/);
});

test("state survives a reload; Reset demo restores the shipped history", async ({ page }) => {
  const a = find((x) => x.status === "proposed" && !x.blocked);
  test.skip(!a, "needs a proposed action");
  await workAs(page, "Dan Okafor");
  await page.goto(`/#/actions/${a!.action_id}`);
  await page.getByTestId("transition-submit").click();
  await expect(page.getByText(label("under_review")).first()).toBeVisible();
  // Survives a full reload.
  await page.reload();
  await expect(page.getByText(label("under_review")).first()).toBeVisible();
  // Reset restores the shipped demo state.
  await page.goto("/#/actions");
  await page.getByTestId("sandbox-reset").click();
  await page.goto(`/#/actions/${a!.action_id}`);
  await expect(page.getByTestId("transition-submit")).toBeVisible();
});

test("the exported sample price file uses the CLI template", async ({ page }) => {
  await workAs(page, "Dan Okafor");
  const exportable = bundle.actions_table.filter(
    (x) =>
      ["price_change", "below_floor_fix"].includes(x.action_type) &&
      ["approved", "exported"].includes(x.status) &&
      x.proposed_price_cents !== null,
  );
  test.skip(exportable.length === 0, "needs exportable price actions");
  const downloadP = page.waitForEvent("download");
  await page.getByTestId("export-run").click();
  const download = await downloadP;
  const text = fs.readFileSync((await download.path())!, "utf-8");
  const lines = text.trimEnd().split("\n");
  expect(lines[0]).toBe("ACTION_ID,APPROVAL_ID,CUSTOMER,ITEM,UOM,EFFECTIVE_DATE,PREVIOUS_PRICE,NEW_PRICE");
  expect(lines.length - 1).toBe(exportable.length);
  for (const line of lines.slice(1)) {
    expect(line).toMatch(/^act_[0-9a-f]+,(apr_[0-9a-f]{10})?,/);
  }
});
