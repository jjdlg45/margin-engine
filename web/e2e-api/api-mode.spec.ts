/** Run 4 (30.5/30.7): the UI in API mode against the real BFF. Loading,
 * pagination and lookups go over the network to /api; a simulated upstream
 * failure (injected at the /api/app boundary, the exact payload the API
 * produces when a connector refresh fails) shows the stale-data label. */

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("overview loads entirely from the API", async ({ page }) => {
  const apiCalls: string[] = [];
  const dataFiles: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (url.includes("/api/")) apiCalls.push(url);
    if (url.includes("/data/") && url.endsWith(".json")) dataFiles.push(url);
  });
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  await page.goto("/");
  await expect(page.getByTestId("data-notice")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  expect(apiCalls.some((u) => u.includes("/api/app"))).toBe(true);
  expect(apiCalls.some((u) => u.includes("/api/pages/overview"))).toBe(true);
  // Static bundle files are never fetched in API mode.
  expect(dataFiles).toEqual([]);
  expect(errors).toEqual([]);
});

test("guidance table paginates server-side", async ({ page }) => {
  const tableCalls: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/tables/guidance")) tableCalls.push(req.url());
  });
  await page.goto("/#/guidance");
  await expect(page.getByRole("table")).toBeVisible();
  expect(tableCalls.length).toBeGreaterThan(0);
  expect(tableCalls[0]).toContain("page_size=");
});

test("simulated upstream failure shows the stale-data label", async ({ page }) => {
  await page.route("**/api/app", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    body.refresh_status = {
      fx: {
        refreshed_utc: "2026-10-20T00:00:00+00:00",
        stale: true,
        detail: "Frankfurter unavailable after 3 attempts",
      },
    };
    await route.fulfill({ response: res, json: body });
  });
  await page.goto("/");
  const stale = page.getByTestId("stale-fx");
  await expect(stale).toBeVisible();
  await expect(stale).toContainText("FX data is stale");
});

test("replay data-as-of label shows when present", async ({ page }) => {
  await page.route("**/api/app", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    body.data_as_of = "2009-12-02";
    await route.fulfill({ response: res, json: body });
  });
  await page.goto("/");
  await expect(page.getByTestId("data-as-of")).toContainText("Data as of 2009-12-02");
});
