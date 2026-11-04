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
  "/recommended",
  "/waterfall",
  "/customers",
  "/segments",
  "/guidance",
  "/sensitivity",
  "/opportunity",
  "/actions",
  "/scenarios",
  "/modules",
  "/methodology",
  "/guide",
  "/connections",
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
          // toHaveScreenshot captures repeatedly until two in a row match,
          // and only then compares to the baseline. Capturing a page this
          // tall (methodology is 8,842px) twice does not fit in the default
          // 5s on a loaded machine, and the first capture is taken while the
          // viewport is still being expanded to the full document - which
          // removes the vertical scrollbar and reflows every row ~18px
          // sideways. Measured at 8 workers: consecutive captures differed
          // by 989,574 pixels while the image it finally settles on is
          // within 8,179 of the baseline, i.e. the comparison was right and
          // only the clock was wrong. Failed 8 runs in 10 at 5s.
          timeout: 30_000,
        });
      }
    });
  }
}
