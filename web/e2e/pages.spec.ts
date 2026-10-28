/** Phase 20 verification: every route loads without console errors, and
 * spot-check parity — values the pages display equal the bundle's figures. */

import { expect, test } from "@playwright/test";
import type { WebBundle } from "../src/types/bundle";

const ROUTES = [
  "/",
  "/waterfall",
  "/customers",
  "/segments",
  "/guidance",
  "/sensitivity",
  "/opportunity",
  "/scenarios",
  "/modules",
  "/methodology",
];

function dollars(cents: number): string {
  const sign = cents < 0 ? "−" : "";
  return `${sign}$${(Math.abs(cents) / 100).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

let bundle: WebBundle;
test.beforeAll(async ({ request }) => {
  bundle = (await (await request.get("/data/bundle.json")).json()) as WebBundle;
});

for (const route of ROUTES) {
  test(`route ${route} loads without console errors`, async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(m.text());
    });
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`/#${route}`);
    await expect(page.locator("h1")).toBeVisible();
    await page.waitForTimeout(250);
    expect(errors, errors.join("; ")).toHaveLength(0);
  });
}

test("waterfall page: leakage component parity", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/waterfall");
  const first = bundle.leakage_components_overall[0];
  const row = page.locator("tbody tr").first();
  await expect(row).toContainText(first.component);
  await expect(row).toContainText(dollars(first.amount_cents));
});

test("customers page: drill-down shows the customer's bundle figures", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/customers");
  const target = bundle.economics.customers[0];
  const input = page.getByRole("combobox", { name: "Customer" });
  await input.fill(target.customer_id);
  await page.getByRole("option").first().click();
  // The row button, not the filter chip (both carry the id in their name).
  await page.locator("tbody").getByRole("button", { name: new RegExp(target.customer_id) }).first().click();
  await expect(page.locator("h2").first()).toContainText(target.customer_id);
  await expect(
    page.getByText(`${dollars(target.net_revenue_cents)} net invoiced revenue`),
  ).toBeVisible();
});

test("guidance page: pair lookup shows bundle floors and explanation", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const pair = bundle.guidance.find((g) => g.status === "below_floor") ?? bundle.guidance[0];
  await page.goto(`/#/guidance?customer=${pair.customer_id}&item=${pair.item_id}`);
  if (pair.pocket_floor_cents !== null && pair.pocket_floor_cents !== undefined) {
    await expect(page.getByText(dollars(pair.pocket_floor_cents)).first()).toBeVisible();
  }
  await expect(page.getByText(pair.explanation.slice(0, 60))).toBeVisible();
});

test("segments page: the later-period test is shown with its baseline", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/segments");
  const h = bundle.segments.holdout;
  expect(h).toBeTruthy();
  await expect(page.getByRole("heading", { name: "Tested on later periods" })).toBeVisible();
  // The segmentation is never shown winning without the thing it beat.
  await expect(page.getByText("Average error of the segment medians")).toBeVisible();
  await expect(page.getByText("Average error of a single median")).toBeVisible();
  const notes = page.getByTestId("segment-notes");
  await expect(notes).toBeVisible();
  for (const n of bundle.segments.notes ?? []) {
    await expect(notes).toContainText(n.slice(0, 60));
  }
});

test("modules page: the win curves say what they modeled and what they left out", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/modules");
  const intake = bundle.optional_modules.quote_intake;
  expect(intake).toBeTruthy();
  // Every quote row is accounted for: modeled plus excluded equals the total.
  const excluded = intake!.excluded.reduce((a, e) => a + e.n, 0);
  expect(intake!.n_modeled + excluded).toBe(intake!.n_rows);
  await expect(
    page.getByText("Only quotes with a decision - won or lost - are evidence", {
      exact: false,
    }),
  ).toBeVisible();
  if (intake!.excluded.length > 0) {
    const list = page.getByTestId("quote-exclusions");
    await expect(list).toBeVisible();
    for (const e of intake!.excluded) {
      await expect(list).toContainText(e.reason);
    }
  }
  // The precision gate reads independent groups, so the page shows them.
  await expect(page.getByText("Independent groups", { exact: true })).toBeVisible();
  await expect(page.getByText("Held-out check", { exact: true })).toBeVisible();
});

test("sensitivity page: segment rows match the bundle", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/sensitivity");
  // Viewers see display ranks ("Segment 1"), never leaf ids.
  const rankOf = new Map(bundle.segments.rules.map((r) => [r.leaf_id, r.display_rank]));
  for (const r of bundle.sensitivity) {
    const label = `Segment ${rankOf.get(r.segment) ?? r.segment}`;
    await expect(page.getByText(label, { exact: false }).first()).toBeVisible();
  }
  const causal = bundle.sensitivity.filter((r) => r.status === "causal_elasticity_supported");
  await expect(page.locator(".chip-solid")).toHaveCount(causal.length);
});

test("scenarios page: slider snaps to the exported grid points", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/scenarios");
  const grid = bundle.scenario_grids[0];
  // The engine exports one point per 1% of pass-through (0..100%).
  expect(grid.points.length).toBe(101);
  expect(grid.points.map((p) => p.pass_through)).toEqual(
    Array.from({ length: 101 }, (_, k) => k / 100),
  );
  // Default position is the 50% grid point.
  const mid = Math.round((grid.points.length - 1) / 2);
  await expect(page.getByText(dollars(grid.points[mid].recovered_cents)).first()).toBeVisible();
  const slider = page.getByRole("slider");
  await slider.focus();
  await page.keyboard.press("End");
  await expect(
    page.getByText(dollars(grid.points[grid.points.length - 1].recovered_cents)).first(),
  ).toBeVisible();
  // One arrow step moves exactly one 1% grid point.
  await page.keyboard.press("ArrowLeft");
  await expect(
    page.getByText(dollars(grid.points[grid.points.length - 2].recovered_cents)).first(),
  ).toBeVisible();
  await page.keyboard.press("Home");
  await expect(page.getByText(dollars(grid.points[0].incremental_cost_cents)).first()).toBeVisible();
});

test("methodology page: validation rows match the bundle", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/methodology");
  for (const v of bundle.validation_results.slice(0, 6)) {
    await expect(page.getByText(v.check, { exact: false }).first()).toBeVisible();
  }
});

test("modules page: present modules render, absent ones state the reason", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/modules");
  const m = bundle.optional_modules;
  if (m.win_curves) {
    await expect(page.getByText("Win rate")).toBeVisible();
  } else if (m.win_curves_absent_reason) {
    await expect(page.getByText(m.win_curves_absent_reason)).toBeVisible();
  }
  if (m.inventory) {
    await expect(page.getByText(m.inventory.method).first()).toBeVisible();
  } else if (m.inventory_absent_reason) {
    await expect(page.getByText(m.inventory_absent_reason)).toBeVisible();
  }
});
