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
  const input = page.getByPlaceholder("Filter customers");
  await input.fill(id);
  await expect(page).toHaveURL(new RegExp(`filter=${id}`));
  // A fresh load of the same URL restores the filtered table.
  await page.goto(`/#/customers?filter=${id}`);
  await expect(page.getByPlaceholder("Filter customers")).toHaveValue(id);
  const rows = page.locator("tbody tr");
  await expect(rows.first()).toContainText(id);
});

test("waterfall scope and group live in the URL", async ({ page }) => {
  await page.goto("/#/waterfall");
  await page.getByRole("tab", { name: "By segment" }).click();
  await expect(page).toHaveURL(/view=segment/);
  const select = page.locator("#segment-pick");
  const options = select.locator("option");
  const n = await options.count();
  expect(n).toBeGreaterThan(0);
  if (n >= 2) {
    // Picking a different group writes it to the URL.
    const second = await options.nth(1).getAttribute("value");
    await select.selectOption(second!);
    await expect(page).toHaveURL(new RegExp(`key=${second}`));
    await page.goto(`/#/waterfall?view=segment&key=${second}`);
    await expect(page.locator("#segment-pick")).toHaveValue(second!);
  } else {
    // Single-segment data: the lone option is active and its deep link
    // restores the same view - no second option to select.
    const only = await options.first().getAttribute("value");
    await expect(select).toHaveValue(only!);
    await page.goto(`/#/waterfall?view=segment&key=${only}`);
    await expect(page.locator("#segment-pick")).toHaveValue(only!);
    await expect(page.locator("svg.recharts-surface").first()).toBeVisible();
  }
});

test("guidance lookup drives the band chart through the selects", async ({ page }) => {
  const pair = bundle.guidance[0];
  await page.goto("/#/guidance");
  await page.locator("#g-cust").selectOption(pair.customer_id);
  await expect(page).toHaveURL(new RegExp(`customer=${pair.customer_id}`));
  await page.locator("#g-item").selectOption(pair.item_id);
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
