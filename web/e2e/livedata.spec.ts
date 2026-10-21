/** Phase 23.9 verification: the live-ready data layer. Network interception
 * proves the loading, error and retry behavior; file-size checks cover the
 * split per-page / per-table files the app consumes (bundle.json and
 * schema.json are tooling/parity artifacts, not fetched by the app); the
 * virtualized guidance table keeps only visible rows in the DOM. */

import { expect, test } from "@playwright/test";

/** Every file the app can fetch. bundle.json / schema.json are excluded on
 * purpose: they exist for tooling and parity tests, the app never loads them. */
const APP_FILES = [
  "app.json",
  "overview.json",
  "waterfall.json",
  "customers.json",
  "segments.json",
  "guidance.json",
  "sensitivity.json",
  "opportunity.json",
  "scenarios.json",
  "methodology.json",
  "modules.json",
  "tables/guidance.json",
  "tables/guidance_explanations.json",
  "tables/opportunities.json",
  "tables/customers.json",
];

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("every app data file is under 1 MB", async ({ request }) => {
  for (const f of APP_FILES) {
    const res = await request.get(`/data/${f}`);
    expect(res.ok(), `${f} must exist`).toBe(true);
    const size = (await res.body()).length;
    expect(size, `${f} is ${(size / 1e6).toFixed(2)} MB`).toBeLessThan(1024 * 1024);
  }
});

test("slow responses show skeletons, then the content", async ({ page }) => {
  await page.route("**/data/overview.json", async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.continue();
  });
  await page.goto("/#/");
  // While the overview payload is delayed, its sections show skeletons.
  await expect(page.getByRole("status").first()).toBeVisible();
  // When it lands, the findings render and the skeletons go away.
  await expect(page.getByText("of pricing leakage between list and pocket")).toBeVisible({
    timeout: 10_000,
  });
});

test("failed fetches show a plain-English error with Retry; Retry recovers", async ({ page }) => {
  let failures = 0;
  await page.route("**/data/waterfall.json", async (route) => {
    // Fail the initial request and both automatic retries; succeed after that,
    // so only the user-driven Retry can recover.
    if (failures < 3) {
      failures += 1;
      await route.fulfill({ status: 500, body: "server error" });
      return;
    }
    await route.continue();
  });
  await page.goto("/#/waterfall");
  const alert = page.getByRole("alert").first();
  await expect(alert).toBeVisible({ timeout: 15_000 });
  await expect(alert).toContainText("This section could not load.");
  await expect(failures, "initial request plus two capped retries").toBe(3);
  await alert.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("tab", { name: "By segment" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("retries stop at the cap on persistent failure", async ({ page }) => {
  let requests = 0;
  await page.route("**/data/sensitivity.json", async (route) => {
    requests += 1;
    await route.fulfill({ status: 500, body: "server error" });
  });
  await page.goto("/#/sensitivity");
  await expect(page.getByRole("alert").first()).toBeVisible({ timeout: 15_000 });
  // Give any runaway retry loop time to show itself, then assert the cap.
  await page.waitForTimeout(2500);
  expect(requests, "1 initial + 2 retries, never more").toBe(3);
});

test("4xx responses are not retried", async ({ page }) => {
  let requests = 0;
  await page.route("**/data/scenarios.json", async (route) => {
    requests += 1;
    await route.fulfill({ status: 404, body: "not found" });
  });
  await page.goto("/#/scenarios");
  await expect(page.getByRole("alert").first()).toBeVisible({ timeout: 10_000 });
  await page.waitForTimeout(1500);
  expect(requests, "a 4xx is terminal - no retries").toBe(1);
});

test("virtualized guidance table renders only visible rows", async ({ page, request }) => {
  const table = (await (await request.get("/data/tables/guidance.json")).json()) as {
    items: unknown[];
  };
  test.skip(table.items.length <= 100, "needs more than 100 rows to prove virtualization");
  await page.goto("/#/guidance");
  const region = page.getByRole("region", { name: "Guidance pairs table" });
  await expect(region).toBeVisible();
  const rendered = await region.locator("tbody tr:not([aria-hidden='true'])").count();
  expect(rendered, `rendered ${rendered} of ${table.items.length}`).toBeLessThan(
    Math.min(80, table.items.length / 2),
  );
  // The full filtered set is still reported, not truncated.
  await expect(page.getByText(/Showing [\d,]+ of [\d,]+/)).toContainText(
    `of ${table.items.length.toLocaleString("en-US")}`,
  );
});

test("combobox option lists of 100+ entries are virtualized", async ({ page, request }) => {
  const facets = (await (await request.get("/data/guidance.json")).json()) as {
    facets: { items: unknown[] };
  };
  test.skip(facets.facets.items.length < 100, "needs 100+ options to prove virtualization");
  await page.goto("/#/guidance");
  const input = page.getByRole("combobox", { name: "Item" });
  await input.click();
  await expect(page.getByRole("option").first()).toBeVisible();
  const rendered = await page.getByRole("option").count();
  expect(rendered, `rendered ${rendered} of ${facets.facets.items.length} options`).toBeLessThan(
    Math.min(50, facets.facets.items.length / 2),
  );
});

test("the Overview loads only app and overview data", async ({ page }) => {
  const fetched: string[] = [];
  await page.route("**/data/**", async (route) => {
    fetched.push(new URL(route.request().url()).pathname.replace(/^.*\/data\//, ""));
    await route.continue();
  });
  await page.goto("/#/");
  await expect(page.getByText("of pricing leakage between list and pocket")).toBeVisible();
  const unexpected = fetched.filter((f) => !["app.json", "overview.json"].includes(f));
  expect(unexpected, `Overview fetched extra files: ${unexpected.join(", ")}`).toHaveLength(0);
});
