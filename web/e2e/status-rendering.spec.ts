/** Phase 46: every guidance, sensitivity, opportunity and action status that
 * appears in the data renders with its dictionary label and a plain-English
 * explanation (the chip's accessible name carries both) - never a raw code. */

import { expect, test } from "@playwright/test";
import type { WebBundle } from "../src/types/bundle";

let bundle: WebBundle;
test.beforeAll(async ({ request }) => {
  bundle = (await (await request.get("/data/bundle.json")).json()) as WebBundle;
});

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

function entry(status: string) {
  const e = bundle.labels[status];
  expect(e, `label dictionary entry for "${status}"`).toBeTruthy();
  expect(e.label).not.toContain("_");
  expect(e.description.length).toBeGreaterThan(20);
  return e;
}

async function expectChip(page: import("@playwright/test").Page, status: string) {
  const e = entry(status);
  const chip = page.getByRole("button", { name: `${e.label}. ${e.description}`, exact: false });
  await expect(chip.first(), `a chip for "${status}" labeled "${e.label}"`).toBeVisible();
}

test("every guidance status renders with label and explanation", async ({ page }) => {
  const statuses = [...new Set(bundle.guidance.map((g) => g.status))].sort();
  expect(statuses.length).toBeGreaterThan(3);
  for (const s of statuses) {
    await page.goto(`/#/guidance?status=${s}`);
    await expectChip(page, s);
  }
});

test("every sensitivity status renders with label and explanation", async ({ page }) => {
  const statuses = [...new Set(bundle.sensitivity.map((r) => r.status))].sort();
  expect(statuses.length).toBeGreaterThan(0);
  await page.goto("/#/sensitivity");
  for (const s of statuses) {
    await expectChip(page, s);
  }
});

test("every opportunity evidence status and volume treatment renders", async ({ page }) => {
  const records = bundle.opportunity.top_opportunities;
  const evidence = [...new Set(records.map((r) => r.evidence_status ?? "insufficient_data"))].sort();
  const treatments = [...new Set(records.map((r) => r.volume_treatment))].sort();
  for (const s of evidence) {
    await page.goto(`/#/opportunity?evidence=${s}`);
    await expectChip(page, s);
  }
  for (const t of treatments) {
    await page.goto(`/#/opportunity?treatment=${t}`);
    await expectChip(page, t);
  }
});

test("every action status and type renders with label and explanation", async ({ page }) => {
  const statuses = bundle.actions.facets.statuses;
  const types = bundle.actions.facets.types;
  expect(statuses.length).toBeGreaterThan(3);
  for (const s of statuses) {
    await page.goto(`/#/actions?statuses=${s}`);
    await expectChip(page, s);
  }
  for (const t of types) {
    await page.goto(`/#/actions?types=${t}`);
    await expectChip(page, t);
  }
});

test("categorical data values render with display casing (acronyms upper-case)", async ({
  page,
}) => {
  await page.goto("/#/segments");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator("main")).not.toContainText("Loading", { timeout: 10_000 });
  const text = await page.locator("main").innerText();
  // The demo mapping declares OEM as an acronym; the generator's industries
  // include oem and construction. Raw lowercase values never reach a reader.
  expect(text).toContain("OEM");
  expect(text).not.toMatch(/\boem\b/);
  expect(text).toMatch(/Construction/);
});
