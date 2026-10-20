/** Phase 22 quality gates: axe on every page (no serious or critical
 * violations) and visual snapshots of every page in light and dark mode,
 * at both project widths.
 *
 * Snapshot baselines are platform-specific (font rasterization differs), and
 * the committed baselines are from the development machine. CI on another
 * platform sets SKIP_SNAPSHOTS=1 to run every other gate without comparing
 * against baselines that cannot exist there - a documented constraint, not a
 * silent skip. */

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

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

const slug = (r: string) => (r === "/" ? "overview" : r.slice(1));

for (const route of ROUTES) {
  for (const theme of ["light", "dark"] as const) {
    test(`axe + snapshot: ${slug(route)} (${theme})`, async ({ page }) => {
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.goto(`/#${route}`);
      await page.evaluate((t) => {
        document.documentElement.dataset.theme = t;
      }, theme);
      await expect(page.locator("h1")).toBeVisible();
      await page.waitForLoadState("networkidle");
      // Chart pages: wait for the rendered surface, not a fixed delay.
      if ((await page.locator("svg.recharts-surface").count()) > 0) {
        await expect(page.locator("svg.recharts-surface").first()).toBeVisible();
      }
      await page.waitForTimeout(400);

      const axe = await new AxeBuilder({ page }).analyze();
      const serious = axe.violations.filter(
        (v) => v.impact === "serious" || v.impact === "critical",
      );
      expect(
        serious,
        serious.map((v) => `${v.id}: ${v.nodes.length} nodes`).join("; "),
      ).toHaveLength(0);

      if (!process.env.SKIP_SNAPSHOTS) {
        await expect(page).toHaveScreenshot(`${slug(route)}-${theme}.png`, {
          fullPage: true,
          maxDiffPixelRatio: 0.02,
        });
      }
    });
  }
}
