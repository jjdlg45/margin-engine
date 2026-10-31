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

test("every opportunity verdict renders; technical statuses one click away", async ({
  page,
}) => {
  const records = bundle.opportunity.top_opportunities;
  const verdicts = [...new Set(records.map((r) => r.verdict ?? "upside_only"))].sort();
  expect(verdicts.length).toBeGreaterThan(1);
  // Each verdict renders in the How sure column, reached through the
  // existing filters (the verdict is derived from them, so filtering by the
  // technical fields still partitions the table).
  const sampleFor = (v: string) => records.find((r) => (r.verdict ?? "upside_only") === v)!;
  for (const v of verdicts) {
    const r = sampleFor(v);
    await page.goto(`/#/opportunity?customer=${r.customer_id}&item=${r.item_id}`);
    await expectChip(page, v);
    // The decision leads the row and the size line carries its own caveat.
    await expect(page.locator("tbody").getByText(r.decision ?? "", { exact: false }).first())
      .toBeVisible();
    // The technical statuses moved into the expandable details - present, one
    // click away, never a column a reader must combine. The click must leave
    // the details OPEN; a settle that wiped it would lose a reader's click
    // too, so the open state is asserted rather than assumed.
    const summary = page.locator("tbody details summary").first();
    await expect(summary).toBeVisible();
    await summary.click();
    await expect(page.locator("tbody details[open]")).toHaveCount(1);
    await expectChip(page, r.evidence_status ?? "insufficient_data");
    await expectChip(page, r.volume_treatment);
  }
  // The rule behind the verdict: a row with no forecast never wears the
  // solid supported treatment.
  const upside = records.find((r) => (r.verdict ?? "upside_only") === "upside_only");
  if (upside) {
    await page.goto(`/#/opportunity?customer=${upside.customer_id}&item=${upside.item_id}`);
    const chip = page.locator("tbody .chip").filter({ hasText: "Upside only" }).first();
    await expect(chip).toBeVisible();
    await expect(chip).not.toHaveClass(/chip-solid/);
  }
});

test("a staged first step names itself on the recommended queue", async ({ page }) => {
  const staged = bundle.actions_table.find(
    (a) => a.staged_step && ["proposed", "under_review", "approved"].includes(a.status),
  );
  test.skip(!staged, "no open staged-step action in this world");
  await page.goto("/#/recommended");
  await expect(page.getByTestId("recommended-list")).toBeVisible();
  await page.goto(`/#/actions/${staged!.action_id}`);
  await expect(page.getByTestId("staged-step-note")).toContainText("step 1 of the move to target");
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
