/** Phase 19 verification: the Overview loads with no console errors, axe
 * reports no serious or critical violations in either theme, and light/dark
 * screenshots are saved to docs/ui/review/ for the owner's visual review. */

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

// Test-run screenshots land in an ignored folder; the committed review set in
// docs/ui/review/ is regenerated only by `npm run review-shots`.
const SHOT_DIR = "screenshots";

for (const theme of ["light", "dark"] as const) {
  test(`overview renders clean in ${theme} mode`, async ({ page }, testInfo) => {
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => consoleErrors.push(String(err)));

    // Full-page capture re-lays-out the responsive chart; reduced motion keeps
    // the deliberate load animation for real users but stabilizes screenshots.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");
    await page.evaluate((t) => {
      document.documentElement.dataset.theme = t;
    }, theme);
    // The waterfall is the signature visual; wait for its bars.
    await expect(page.getByRole("heading", { name: "Every dollar accounted for." })).toBeVisible();
    await expect(page.locator("svg.recharts-surface").first()).toBeVisible();
    await expect(page.getByText("Synthetic demo data", { exact: false }).first()).toBeVisible();
    await page.waitForTimeout(900); // let the single build animation finish

    expect(consoleErrors, `console errors: ${consoleErrors.join("; ")}`).toHaveLength(0);

    const axe = await new AxeBuilder({ page }).analyze();
    const serious = axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(
      serious,
      serious.map((v) => `${v.id}: ${v.nodes.length} nodes`).join("; "),
    ).toHaveLength(0);

    await page.screenshot({
      path: `${SHOT_DIR}/overview-${theme}-${testInfo.project.name}.png`,
      fullPage: true,
    });
  });
}

test("detail routes render real pages", async ({ page }) => {
  await page.goto("/#/guidance");
  await expect(page.getByRole("heading", { name: "Price guidance" })).toBeVisible();
});
