/** Phase 21 verification: filters, drill-downs, lookup and slider as
 * interactions, with the URL reflecting state (shareable links). */

import { expect, test } from "@playwright/test";
import type { WebBundle } from "../src/types/bundle";

let bundle: WebBundle;
test.beforeAll(async ({ request }) => {
  bundle = (await (await request.get("/data/bundle.json")).json()) as WebBundle;
});

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("customer filter is shareable via the URL", async ({ page }) => {
  const id = bundle.economics.customers[0].customer_id;
  await page.goto("/#/customers");
  const input = page.getByRole("combobox", { name: "Customer" });
  await input.fill(id);
  await page.getByRole("option").first().click();
  await expect(page).toHaveURL(new RegExp(`cust=${id}`));
  // A fresh load of the same URL restores the filtered table.
  await page.goto(`/#/customers?cust=${id}`);
  const rows = page.locator("tbody tr");
  await expect(rows.first()).toContainText(id);
});

test("waterfall scope and group live in the URL", async ({ page }) => {
  await page.goto("/#/waterfall");
  await page.getByRole("tab", { name: "By segment" }).click();
  await expect(page).toHaveURL(/view=segment/);
  // Data-agnostic: pick the second option when one exists, else assert the
  // single-option behavior - through the accessible combobox, no native select.
  const combo = page.getByRole("combobox", { name: "Segment" });
  await combo.fill("Segment");
  const options = page.getByRole("option");
  await expect(options.first()).toBeVisible();
  const n = await options.count();
  await (n >= 2 ? options.nth(1) : options.first()).click();
  await expect(page).toHaveURL(/key=/);
  const key = new URL(page.url().replace("/#/", "/")).searchParams.get("key");
  // Deep link restores the same view and renders the chart.
  await page.goto(`/#/waterfall?view=segment&key=${key}`);
  await expect(page.getByRole("combobox", { name: "Segment" })).toHaveValue(/Segment/);
  await expect(page.locator("svg.recharts-surface").first()).toBeVisible();
});

test("guidance lookup drives the band chart through the comboboxes", async ({ page }) => {
  const pair = bundle.guidance[0];
  await page.goto("/#/guidance");
  const cust = page.getByRole("combobox", { name: "Customer" });
  await cust.fill(pair.customer_id);
  await page.getByRole("option").first().click();
  await expect(page).toHaveURL(new RegExp(`customer=${pair.customer_id}`));
  const item = page.getByRole("combobox", { name: "Item" });
  await item.fill(pair.item_id);
  await page.getByRole("option").first().click();
  await expect(page).toHaveURL(new RegExp(`item=${pair.item_id}`));
  await expect(page.locator("svg[role='img']").first()).toBeVisible();
});

test("keyboard: tab reaches the nav and focus is visible", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("Tab"); // skip link
  await expect(page.getByText("Skip to content")).toBeFocused();
  await page.keyboard.press("Tab");
  const focused = page.locator(":focus");
  await expect(focused).toBeVisible();
  const outline = await focused.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe("none");
});

test("dark mode follows the system setting; the Theme button overrides", async ({ page }) => {
  // Theme changes settle a frame later (the reduced-motion rule turns every
  // property change into a 0.01 ms transition), so assertions must retry:
  // toHaveCSS polls, a bare getComputedStyle read races.
  const body = page.locator("body");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/#/");
  const darkBg = await body.evaluate((el) => getComputedStyle(el).backgroundColor);
  await page.emulateMedia({ colorScheme: "light" });
  await expect(body, "system light renders differently").not.toHaveCSS(
    "background-color",
    darkBg,
  );
  const lightBg = await body.evaluate((el) => getComputedStyle(el).backgroundColor);
  // The Theme button overrides the system preference, both ways.
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(body).toHaveCSS("background-color", darkBg);
  const toggle = page.getByRole("button", { name: "Toggle light and dark theme" });
  await toggle.click();
  await expect(body, "one click from system-dark gives light").toHaveCSS(
    "background-color",
    lightBg,
  );
  await toggle.click();
  await expect(body, "a second click returns to dark").toHaveCSS("background-color", darkBg);
});
