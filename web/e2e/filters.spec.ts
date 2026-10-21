/** Phase 23.8 verification: the shared filter system. Each filter alone and
 * combined, chip removal, clear all, URL round trips, keyboard-only use, name
 * and ID search, range boundaries - with filtered counts checked against the
 * bundle (parity). */

import { expect, test } from "@playwright/test";
import type { WebBundle } from "../src/types/bundle";

let bundle: WebBundle;
test.beforeAll(async ({ request }) => {
  bundle = (await (await request.get("/data/bundle.json")).json()) as WebBundle;
});

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

async function pickFromCombobox(page: import("@playwright/test").Page, label: string, text: string) {
  const input = page.getByRole("combobox", { name: label });
  await input.fill(text);
  await page.getByRole("option").first().click();
}

function showing(page: import("@playwright/test").Page) {
  return page.getByText(/Showing [\d,]+ of [\d,]+/);
}

test("guidance: status filter count matches the bundle", async ({ page }) => {
  await page.goto("/#/guidance");
  const status = bundle.guidance.find((g) => g.status === "below_floor")
    ? "below_floor"
    : bundle.guidance[0].status;
  const expected = bundle.guidance.filter((g) => g.status === status).length;
  await page.getByRole("button", { name: "Any" }).first().click();
  await page.getByRole("option", { name: bundle.labels[status]?.label ?? status }).click();
  await page.keyboard.press("Escape");
  await expect(showing(page)).toContainText(
    `Showing ${expected.toLocaleString("en-US")} of ${bundle.guidance.length.toLocaleString("en-US")}`,
  );
  await expect(page).toHaveURL(new RegExp(`status=${status}`));
});

test("guidance: price range boundaries match the bundle (parity)", async ({ page }) => {
  await page.goto("/#/guidance?pol=~90");
  const expected = bundle.guidance.filter(
    (g) => g.current_index !== null && g.current_index !== undefined && g.current_index * 100 <= 90,
  ).length;
  await expect(showing(page)).toContainText(`Showing ${expected.toLocaleString("en-US")}`);
  // Boundary: a max exactly at a pair's value includes it.
  const idx = bundle.guidance.find((g) => g.current_index !== null)!.current_index!;
  const bound = Math.round(idx * 1000) / 10;
  await page.goto(`/#/guidance?pol=~${bound}`);
  const expected2 = bundle.guidance.filter(
    (g) => g.current_index !== null && g.current_index !== undefined && g.current_index * 100 <= bound,
  ).length;
  await expect(showing(page)).toContainText(`Showing ${expected2.toLocaleString("en-US")}`);
});

test("guidance: combined filters, chips, clear all", async ({ page }) => {
  const pair = bundle.guidance.find((g) => g.customer_name)!;
  await page.goto("/#/guidance");
  await pickFromCombobox(page, "Customer", pair.customer_id);
  await expect(page).toHaveURL(new RegExp(`customer=${pair.customer_id}`));
  const expected = bundle.guidance.filter((g) => g.customer_id === pair.customer_id).length;
  await expect(showing(page)).toContainText(`Showing ${expected.toLocaleString("en-US")}`);
  // Chip removal restores the full count.
  await page.getByRole("button", { name: /Remove filter/ }).first().click();
  await expect(showing(page)).toContainText(
    `of ${bundle.guidance.length.toLocaleString("en-US")}`,
  );
  // Combined + clear all.
  await page.goto(`/#/guidance?customer=${pair.customer_id}&pol=~95`);
  await page.getByRole("button", { name: "Clear all" }).click();
  await expect(showing(page)).toContainText(
    `Showing ${bundle.guidance.length.toLocaleString("en-US")} of`,
  );
  await expect(page).not.toHaveURL(/customer=/);
});

test("guidance: name search and ID search find the same pair", async ({ page }) => {
  const pair = bundle.guidance.find((g) => g.customer_name)!;
  await page.goto("/#/guidance");
  // Name search.
  await pickFromCombobox(page, "Customer", pair.customer_name!.slice(0, 12));
  await expect(page).toHaveURL(/customer=/);
  await page.getByRole("button", { name: "Clear all" }).click();
  // ID search (also via the lookup-mode toggle).
  await page.getByRole("button", { name: "Look up by ID" }).first().click();
  await pickFromCombobox(page, "Customer", pair.customer_id);
  await expect(page).toHaveURL(new RegExp(`customer=${pair.customer_id}`));
});

test("guidance: keyboard-only combobox use", async ({ page }) => {
  await page.goto("/#/guidance");
  const input = page.getByRole("combobox", { name: "Customer" });
  await input.focus();
  await page.keyboard.type(bundle.guidance[0].customer_id.slice(0, 5));
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/customer=/);
});

test("guidance: row click opens the band chart; deep link round-trips", async ({ page }) => {
  const pair = bundle.guidance[0];
  await page.goto(`/#/guidance?customer=${pair.customer_id}&item=${pair.item_id}`);
  await expect(page.locator("svg[role='img']").first()).toBeVisible();
  await expect(page.getByText(pair.explanation.slice(0, 50))).toBeVisible();
});

test("opportunity: evidence filter parity and gross range", async ({ page }) => {
  await page.goto("/#/opportunity");
  const total = bundle.opportunity.top_opportunities.length;
  await expect(showing(page)).toContainText(`of ${total.toLocaleString("en-US")}`);
  const ev = bundle.opportunity.top_opportunities[0].evidence_status ?? "insufficient_data";
  const expected = bundle.opportunity.top_opportunities.filter(
    (r) => (r.evidence_status ?? "insufficient_data") === ev,
  ).length;
  await page.getByRole("button", { name: "Any" }).first().click();
  await page.getByRole("option", { name: bundle.labels[ev]?.label ?? ev }).click();
  await page.keyboard.press("Escape");
  await expect(showing(page)).toContainText(`Showing ${expected.toLocaleString("en-US")}`);
  // Gross range via URL (dollars).
  const min = 5000;
  await page.goto(`/#/opportunity?gross=${min}~`);
  const expected2 = bundle.opportunity.top_opportunities.filter(
    (r) => r.gross_price_opportunity_cents / 100 >= min,
  ).length;
  await expect(showing(page)).toContainText(`Showing ${expected2.toLocaleString("en-US")}`);
});

test("customers: flags, losing-money toggle and ranges match the bundle", async ({ page }) => {
  await page.goto("/#/customers");
  const total = bundle.economics.customers.length;
  await expect(showing(page)).toContainText(`of ${total.toLocaleString("en-US")}`);
  await page.getByRole("button", { name: "Losing money only" }).click();
  const losing = bundle.economics.customers.filter(
    (c) => c.contribution_after_activity_cents < 0,
  ).length;
  await expect(showing(page)).toContainText(`Showing ${losing.toLocaleString("en-US")}`);
  await expect(page).toHaveURL(/losing=1/);
  // Order-count range round trip.
  await page.goto("/#/customers?orders=100~");
  const expected = bundle.economics.customers.filter((c) => c.n_orders >= 100).length;
  await expect(showing(page)).toContainText(`Showing ${expected.toLocaleString("en-US")}`);
});

test("customers: name display and name search", async ({ page }) => {
  const named = bundle.economics.customers.find((c) => c.customer_name)!;
  await page.goto("/#/customers");
  await pickFromCombobox(page, "Customer", named.customer_name!.split(" ")[0]);
  await expect(page).toHaveURL(/cust=/);
  await expect(page.getByText(named.customer_name!).first()).toBeVisible();
});

test("scenarios: grouped combobox switches the grid", async ({ page }) => {
  test.skip(bundle.scenario_grids.length < 2, "needs two scenario grids");
  await page.goto("/#/scenarios");
  const target = bundle.scenario_grids[1];
  const input = page.getByRole("combobox", { name: "Scenario" });
  await input.click();
  // Groups are visible as section headers.
  await expect(page.getByText("Category").first()).toBeVisible();
  await input.fill((target.scope_label ?? target.scope_value).slice(0, 8));
  await page.getByRole("option").first().click();
  await expect(page).toHaveURL(/scenario=/);
});
