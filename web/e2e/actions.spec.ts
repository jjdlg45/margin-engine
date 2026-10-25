/** Phase 42: the Action center in the STATIC demo - read-only with a
 * simulated, labeled history. Queue filters (alone and combined, URL round
 * trip, parity of counts with the bundle), the detail page with history and
 * evidence links, the results view, and the absence of every mutation
 * affordance. */

import { expect, test } from "@playwright/test";
import type { WebBundle } from "../src/types/bundle";

let bundle: WebBundle;
test.beforeAll(async ({ request }) => {
  bundle = (await (await request.get("/data/bundle.json")).json()) as WebBundle;
});

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

const showing = (page: import("@playwright/test").Page) =>
  page.getByText(/Showing [\d,]+ of [\d,]+/);

test("queue renders read-only with counts matching the bundle", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await page.goto("/#/actions");
  await expect(page.getByRole("heading", { level: 1, name: "Action center" })).toBeVisible();
  await expect(page.getByTestId("actions-summary")).toBeVisible();
  await expect(page.getByText(/read-only/)).toBeVisible();
  // No mutation affordances in the static demo.
  await expect(page.getByTestId("actions-toolbar")).toHaveCount(0);
  const total = bundle.actions_table.length;
  await expect(showing(page)).toContainText(`of ${total.toLocaleString("en-US")}`);
  expect(errors).toEqual([]);
});

test("filters: status and compliance, combined, chips and URL round trip", async ({ page }) => {
  await page.goto("/#/actions");
  // Type, Status, Priority, Evidence multi-selects all read "Any" until used.
  await page.getByRole("button", { name: "Any" }).nth(1).click();
  await page.getByRole("option", { name: "Implemented", exact: true }).click();
  await page.keyboard.press("Escape");
  const implemented = bundle.actions_table.filter((a) => a.status === "implemented").length;
  await expect(showing(page)).toContainText(
    `Showing ${implemented.toLocaleString("en-US")} of`,
  );
  expect(page.url()).toContain("statuses=implemented");

  // Combined with the compliance toggle.
  await page.getByRole("button", { name: "Compliance flag only" }).click();
  const both = bundle.actions_table.filter(
    (a) => a.status === "implemented" && a.compliance_flags,
  ).length;
  await expect(showing(page)).toContainText(`Showing ${both.toLocaleString("en-US")} of`);

  // URL round trip restores both filters.
  await page.reload();
  await expect(showing(page)).toContainText(`Showing ${both.toLocaleString("en-US")} of`);

  // Chip removal, then clear all.
  await page.getByRole("button", { name: "Remove filter: Implemented" }).click();
  const flaggedOnly = bundle.actions_table.filter((a) => a.compliance_flags).length;
  await expect(showing(page)).toContainText(
    `Showing ${flaggedOnly.toLocaleString("en-US")} of`,
  );
  await page.getByRole("button", { name: "Clear all" }).click();
  await expect(showing(page)).toContainText(
    `Showing ${bundle.actions_table.length.toLocaleString("en-US")} of`,
  );
});

test("impact range filter matches the bundle at its boundary", async ({ page }) => {
  await page.goto("/#/actions");
  const min = page.getByRole("textbox", { name: "Expected impact min" });
  await min.fill("100");
  await min.press("Enter");
  const want = bundle.actions_table.filter(
    (a) => a.expected_impact_cents !== null && a.expected_impact_cents / 100 >= 100,
  ).length;
  await expect(showing(page)).toContainText(`Showing ${want.toLocaleString("en-US")} of`);
});

test("detail: history labeled demo data, compliance note, evidence links, outcome", async ({
  page,
}) => {
  const flagged = bundle.actions_table.find((a) => a.compliance_flags && a.customer_id);
  expect(flagged).toBeTruthy();
  await page.goto(`/#/actions/${flagged!.action_id}`);
  await expect(page.getByTestId("compliance-note")).toContainText("review");
  await expect(page.getByTestId("event-history")).toBeVisible();
  await expect(page.getByText("Simulated review history - demo data.")).toBeVisible();
  // Evidence deep link to the guidance band for the pair.
  const link = page.getByRole("link", { name: /band chart/ });
  await expect(link).toHaveAttribute(
    "href",
    new RegExp(`guidance\\?customer=${flagged!.customer_id}`),
  );
  // No workflow buttons in the static demo.
  await expect(page.getByTestId("workflow")).toHaveCount(0);

  const measured = bundle.actions_table.find(
    (a) => a.outcome && a.outcome.outcome_status === "measured",
  );
  expect(measured).toBeTruthy();
  await page.goto(`/#/actions/${measured!.action_id}`);
  await expect(page.getByTestId("outcome")).toBeVisible();
  await expect(page.getByText("difference-in-differences")).toBeVisible();
});

test("results view: calibration table and outcomes match the bundle", async ({ page }) => {
  await page.goto("/#/actions");
  await page.getByRole("tab", { name: "Results" }).click();
  const cal = bundle.actions.calibration;
  if (cal.length && cal[0].status !== "not_enough_implemented_actions") {
    for (const row of cal) {
      await expect(
        page.getByRole("cell", { name: row.n_measured.toLocaleString("en-US"), exact: true }).first(),
      ).toBeVisible();
    }
  } else {
    await expect(page.getByText("Not enough implemented actions yet")).toBeVisible();
  }
  const tracked = bundle.actions_table.filter((a) => a.outcome).length;
  const outcomesSection = page
    .locator("section")
    .filter({ hasText: "Realized vs predicted" });
  await expect(outcomesSection.locator("tbody tr")).toHaveCount(tracked);
});

test("summary impact by basis equals the bundle sums (parity)", async ({ page }) => {
  await page.goto("/#/actions");
  const byBasis = new Map<string, number>();
  for (const a of bundle.actions_table) {
    byBasis.set(a.impact_basis, (byBasis.get(a.impact_basis) ?? 0) + (a.expected_impact_cents ?? 0));
  }
  for (const [basis, cents] of Object.entries(bundle.actions.summary.impact_by_basis_cents)) {
    expect(byBasis.get(basis) ?? 0, `summary basis ${basis} ties to rows`).toBe(cents);
    void cents;
  }
  await expect(page.getByText("never summed", { exact: false }).first()).toBeVisible();
});
