/** Connections screen in API mode (Run 7, Phase 94): live status, Check
 * connection, Sync now, and schedule settings against the real BFF with the
 * file-drop connector pointed at fixed e2e folders this spec seeds. */

import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// The API server runs from the repo root (playwright.api.config.ts); this
// spec runs from web/, so the shared folders sit one level up.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..", ".api_state", "e2e-filedrop");
const DROP = path.join(ROOT, "drop");

const CSV =
  "LINE_KEY,INV_NO,INV_DT,CUST_NO,ITEM_NO,QTY_SHP,SELL_UOM,UNIT_PRC,EXT_NET,TXN_CD\n" +
  "L1,INV1,03/15/2024,C1,ITEM1,10,EA,10.00,100.00,S\n" +
  "L2,INV2,03/16/2024,C2,ITEM1,4,EA,10.50,42.00,S\n";

test.beforeAll(() => {
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(DROP, { recursive: true });
  fs.writeFileSync(path.join(DROP, "invoice_lines_e2e.csv"), CSV, "utf-8");
});

test("the connections page exists in API mode navigation", async ({ page }) => {
  await page.goto("/#/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.getByRole("link", { name: "Connections", exact: true })).toBeVisible();
});

test("live status, check, sync now and schedule on the file-drop connector", async ({ page }) => {
  await page.goto("/#/connections");
  await expect(page.getByRole("heading", { level: 1, name: "Connections" })).toBeVisible();

  // Every connector renders with its system name, never a raw code.
  await expect(page.getByText("File drop (watched folder / SFTP)")).toBeVisible();
  await expect(page.getByText("QuickBooks Online")).toBeVisible();
  await expect(page.getByText("HubSpot CRM")).toBeVisible();
  // The honesty label is shown for connectors not verified live.
  await expect(page.getByText(/not yet\s+verified live/).first()).toBeVisible();

  const card = page.locator("article", { hasText: "File drop (watched folder / SFTP)" });
  await expect(card.getByText("No sync has run yet.")).toBeVisible();

  await card.getByRole("button", { name: "Check connection" }).click();
  await expect(card.getByText(/Ready: .*reachable/)).toBeVisible();

  await card.getByRole("button", { name: "Sync now" }).click();
  await expect(card.getByText(/Sync finished/)).toBeVisible({ timeout: 20_000 });
  await expect(card.getByText(/invoice_lines\.csv: 2 rows \(2 new or changed\)/)).toBeVisible();
  await expect(card.getByText(/Last sync /)).toBeVisible();

  // A second sync reports idempotency instead of inventing changes.
  await card.getByRole("button", { name: "Sync now" }).click();
  await expect(card.getByText(/no changes since the last one/)).toBeVisible({ timeout: 20_000 });

  // Schedule settings persist through the API.
  await card.getByLabel(/Sync interval in minutes/).fill("30");
  await card.getByRole("button", { name: "Save schedule" }).click();
  await expect(card.getByText(/Schedule saved: every 30 minutes/)).toBeVisible();
});

test("a failing connector shows the failure in plain English", async ({ page }) => {
  await page.goto("/#/connections");
  const card = page.locator("article", { hasText: "HubSpot CRM" });
  await expect(card.getByText("HUBSPOT_TOKEN")).toBeVisible();
  await card.getByRole("button", { name: "Check connection" }).click();
  await expect(card.getByText(/Not ready: Missing settings/)).toBeVisible();
});
