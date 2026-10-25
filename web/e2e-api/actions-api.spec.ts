/** Phase 42: the Action center in API mode against the real BFF. Every
 * lifecycle transition driven through the UI, role restrictions and the
 * self-approval block surfaced verbatim from the API, the price export, and
 * an optimistic update rolled back on a simulated failure. Identity is a
 * configured user name (local identity, not authentication). */

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

async function pickUser(page: import("@playwright/test").Page, name: string) {
  await page.getByRole("combobox", { name: "Working as" }).selectOption({ value: name });
}

/** A proposed, unblocked action id straight from the API (each run consumes a
 * few; the seeded demo world provides hundreds). */
async function freshProposedId(
  request: import("@playwright/test").APIRequestContext,
  opts: { flagged?: boolean; priceType?: boolean } = {},
): Promise<string> {
  const res = await request.get(
    "http://127.0.0.1:8321/api/actions?page=1&page_size=5000" +
      `&filters=${encodeURIComponent(JSON.stringify({ statuses: ["proposed"] }))}`,
  );
  const body = (await res.json()) as {
    items: {
      action_id: string;
      blocked: boolean;
      compliance_flags: string;
      action_type: string;
      proposed_price_cents: number | null;
    }[];
  };
  const row = body.items.find(
    (r) =>
      !r.blocked &&
      (opts.flagged === undefined || Boolean(r.compliance_flags) === opts.flagged) &&
      (!opts.priceType || (r.proposed_price_cents !== null && r.action_type === "price_change")),
  );
  expect(row, "a suitable proposed action exists").toBeTruthy();
  return row!.action_id;
}

test("full lifecycle: submit, self-approval blocked, approve, export, implement", async ({
  page,
  request,
}) => {
  const id = await freshProposedId(request, { flagged: false, priceType: true });
  await page.goto("/#/actions");
  await pickUser(page, "demo-lead");
  await page.goto(`/#/actions/${id}`);
  await page.getByTestId("transition-submit").click();
  await expect(page.getByTestId("transition-approve")).toBeVisible();

  // Separation of duties: demo-lead submitted it and cannot approve it.
  await page.getByTestId("decision-note").fill("fits policy");
  await page.getByTestId("transition-approve").click();
  await expect(page.getByTestId("workflow-error")).toContainText("separation of duties");
  // The optimistic status rolled back to under review.
  await expect(page.getByText("Under review").first()).toBeVisible();

  await page.goto("/#/actions");
  await pickUser(page, "demo-approver");
  await page.goto(`/#/actions/${id}`);
  await page.getByTestId("decision-note").fill("fits policy");
  await page.getByTestId("transition-approve").click();
  await expect(page.getByText("price export", { exact: false }).first()).toBeVisible();

  // Export moves approved price actions to exported (analyst runs it).
  await page.goto("/#/actions");
  await pickUser(page, "demo-analyst");
  const download = page.waitForEvent("download");
  await page.getByTestId("export-run").click();
  await expect(page.getByTestId("export-summary")).toContainText("moved to exported");
  await (await download).cancel();

  await page.goto(`/#/actions/${id}`);
  await expect(page.getByTestId("transition-implement")).toBeVisible();
  await page.getByTestId("transition-implement").click();
  await expect(page.getByText("Implemented").first()).toBeVisible();
});

test("reject and rework; viewer may not transition", async ({ page, request }) => {
  const id = await freshProposedId(request, { flagged: false });
  await page.goto("/#/actions");
  await pickUser(page, "demo-viewer");
  await page.goto(`/#/actions/${id}`);
  await page.getByTestId("transition-submit").click();
  await expect(page.getByTestId("workflow-error")).toContainText("role");

  await page.goto("/#/actions");
  await pickUser(page, "demo-analyst");
  await page.goto(`/#/actions/${id}`);
  await page.getByTestId("transition-submit").click();
  await page.goto("/#/actions");
  await pickUser(page, "demo-approver");
  await page.goto(`/#/actions/${id}`);
  await page.getByTestId("decision-note").fill("not yet");
  await page.getByTestId("transition-reject").click();
  await expect(page.getByTestId("transition-rework")).toBeVisible();
  await page.goto("/#/actions");
  await pickUser(page, "demo-analyst");
  await page.goto(`/#/actions/${id}`);
  await page.getByTestId("transition-rework").click();
  await expect(page.getByTestId("transition-submit")).toBeVisible();
  // The event log carries the whole walk (the demo store persists between
  // local runs, so assert the latest cycle rather than an absolute count).
  const events = page.getByTestId("event-history").locator("li");
  await expect(events.last()).toContainText("Proposed");
  await expect(events.nth(-2)).toContainText("Rejected");
  await expect(events.nth(-2)).toContainText("not yet");
});

test("approving a flagged action demands a written justification", async ({ page, request }) => {
  const id = await freshProposedId(request, { flagged: true });
  await page.goto("/#/actions");
  await pickUser(page, "demo-analyst");
  await page.goto(`/#/actions/${id}`);
  await expect(page.getByTestId("compliance-note")).toBeVisible();
  await page.getByTestId("transition-submit").click();
  await page.goto("/#/actions");
  await pickUser(page, "demo-approver");
  await page.goto(`/#/actions/${id}`);
  await expect(page.getByText("Written justification (required", { exact: false })).toBeVisible();
  await page.getByTestId("transition-approve").click();
  await expect(page.getByTestId("workflow-error")).toContainText("justification");
  await page.getByTestId("decision-note").fill("Cost-to-serve difference documented on the compliance detail.");
  await page.getByTestId("transition-approve").click();
  await expect(page.getByText("price export", { exact: false }).first()).toBeVisible();
});

test("optimistic update rolls back when the API fails", async ({ page, request }) => {
  const id = await freshProposedId(request);
  await page.goto("/#/actions");
  await pickUser(page, "demo-analyst");
  await page.goto(`/#/actions/${id}`);
  await page.route("**/api/actions/*/submit", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ code: "boom", detail: "simulated outage" }),
    }),
  );
  await page.getByTestId("transition-submit").click();
  await expect(page.getByTestId("workflow-error")).toContainText("simulated outage");
  // Rolled back: still proposed, submit still offered.
  await expect(page.getByTestId("transition-submit")).toBeVisible();
  await expect(page.getByText("Proposed").first()).toBeVisible();
});

test("manual proposal appears in the queue", async ({ page }) => {
  await page.goto("/#/actions");
  await pickUser(page, "demo-analyst");
  await page.getByRole("button", { name: "Propose an action" }).click();
  const title = `Review delivery cadence ${Date.now()}`;
  const form = page.getByTestId("propose-form");
  await form.getByLabel("Title").fill(title);
  await form.getByLabel("Rationale", { exact: true }).fill("Requested by the account team.");
  await page.getByRole("button", { name: "Submit proposal" }).click();
  await expect(page.getByTestId("propose-form")).toHaveCount(0);
  // Narrow to this proposer's actions: generated actions are owned by
  // "unassigned", manual proposals by their proposer.
  const owner = page.getByRole("combobox", { name: "Owner" });
  await owner.click();
  await owner.pressSequentially("demo-analyst");
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.getByRole("option").first().click();
  await expect(page.getByRole("link", { name: title })).toBeVisible();
});
