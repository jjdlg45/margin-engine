/** Polish round: grouped navigation. Desktop shows four labeled groups in the
 * header; below 640 px a Menu button opens a collapsible grouped list that
 * closes again on navigation. */

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("desktop navigation shows four labeled groups", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 940 });
  await page.goto("/#/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const group of ["Margin", "Pricing", "Actions", "Data"]) {
    await expect(nav.getByText(group, { exact: true })).toBeVisible();
  }
  // Every page is reachable from its group.
  for (const label of [
    "Overview",
    "Waterfall",
    "Customers & orders",
    "Segments",
    "Guidance",
    "Sensitivity",
    "Opportunity",
    "Scenarios",
    "Action center",
    "Data & methodology",
    "Optional modules",
  ]) {
    await expect(nav.getByRole("link", { name: label, exact: true })).toBeVisible();
  }
  await nav.getByRole("link", { name: "Guidance", exact: true }).click();
  await expect(page).toHaveURL(/#\/guidance/);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
});

test("mobile menu collapses, groups, navigates and closes", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto("/#/");
  const menu = page.getByRole("button", { name: "Menu" });
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0);
  await menu.click();
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const group of ["Margin", "Pricing", "Actions", "Data"]) {
    await expect(nav.getByText(group, { exact: true })).toBeVisible();
  }
  await nav.getByRole("link", { name: "Action center", exact: true }).click();
  await expect(page).toHaveURL(/#\/actions/);
  // The menu closed on navigation.
  await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0);
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  // Keyboard: the menu button is reachable and operable.
  await menu.focus();
  await page.keyboard.press("Enter");
  await expect(menu).toHaveAttribute("aria-expanded", "true");
});

test("action detail carries breadcrumbs and labeled fields, no dot-joined meta", async ({ page }) => {
  const table = (await (await page.request.get("/data/tables/actions.json")).json()) as {
    items: { action_id: string; current_price_cents: number | null }[];
  };
  const a = table.items.find((x) => x.current_price_cents !== null) ?? table.items[0];
  await page.goto(`/#/actions/${a.action_id}`);
  const bc = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(bc).toBeVisible();
  // Labeled fields replace the old dot-separated meta line.
  for (const label of ["Priority", "Owner", "Due date", "Expected impact"]) {
    await expect(page.getByText(label, { exact: true })).toBeVisible();
  }
  if (a.current_price_cents !== null) {
    await expect(page.getByText("Current price", { exact: true })).toBeVisible();
  }
  await bc.getByRole("link", { name: "Action center" }).click();
  await expect(page).toHaveURL(/#\/actions$/);
});

test("customer drill-down breadcrumb returns to the filtered list", async ({ page }) => {
  await page.goto("/#/customers?losing=1");
  // Open the first customer in the (filtered) table.
  await page.locator("tbody button").first().click();
  const bc = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(bc).toBeVisible();
  await bc.getByRole("button", { name: "Customers & orders" }).click();
  // Back on the list, with the losing-money filter still applied.
  await expect(page).toHaveURL(/losing=1/);
  await expect(page.locator("tbody tr").first()).toBeVisible();
});
