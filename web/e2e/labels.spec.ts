/** Phase 23.5 gate, extended: no raw machine codes anywhere a reader can see
 * them - visible text, title-attribute tooltips, aria labels, or the content
 * of an opened status tooltip. Raw rule logic lives only in the engine's
 * CSV/Parquet exports. The allowlist names intentional technical terms
 * (file names, CLI commands, mapping option paths shown as instructions). */

import { expect, test } from "@playwright/test";

const ROUTES = [
  "/",
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
];

// Intentional technical terms allowed in visible text.
const ALLOW = new Set([
  // documented artifact/file names shown as "where listed" instructions
  "reconciliation.csv",
  "unallocated_settlements.csv",
  "methodology.md",
  "bundle.json",
  // CLI commands shown as instructions in the bundle-missing error state
  "margin-engine",
  "export-web",
  "web/public/data/bundle.json",
  // mapping artifact shown in the data label
  "scripts/measure_validation.py",
]);

const SNAKE = /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g;

function violations(text: string): string[] {
  const out = new Set<string>();
  for (const m of text.matchAll(SNAKE)) {
    const tok = m[0];
    const windowed = text.slice(Math.max(0, (m.index ?? 0) - 40), (m.index ?? 0) + tok.length + 8);
    if ([...ALLOW].some((a) => windowed.includes(a))) continue;
    out.add(tok);
  }
  return [...out];
}

for (const route of ROUTES) {
  test(`no raw codes visible on ${route === "/" ? "overview" : route.slice(1)}`, async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`/#${route}`);
    await expect(page.locator("h1")).toBeVisible();
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(300);
    // Visible text, plus every title-attribute tooltip and aria label: a
    // reader hovers titles and a screen reader speaks aria labels, so raw
    // codes are just as leaked there as in rendered text.
    const text = await page.evaluate(() => {
      const parts = [document.body.innerText];
      for (const el of document.querySelectorAll("[title]")) {
        parts.push(el.getAttribute("title") ?? "");
      }
      for (const el of document.querySelectorAll("[aria-label]")) {
        parts.push(el.getAttribute("aria-label") ?? "");
      }
      return parts.join("\n");
    });
    const bad = violations(text);
    expect(bad, `raw codes visible on ${route}: ${bad.join(", ")}`).toHaveLength(0);
  });
}

test("status tooltips show the plain-English description, never a raw code", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#/sensitivity");
  await expect(page.locator("h1")).toBeVisible();
  const trigger = page.locator("button:has(.chip)").first();
  await expect(trigger).toBeVisible();
  await trigger.hover();
  const tooltip = page.getByRole("tooltip").first();
  const content = (await tooltip.textContent()) ?? "";
  expect(content.trim().length, "tooltip must carry a description").toBeGreaterThan(0);
  const bad = violations(content);
  expect(bad, `raw codes in tooltip: ${bad.join(", ")}`).toHaveLength(0);
  expect(content, "no underscores in tooltip text").not.toContain("_");
  expect(content, "no bracketed raw code in tooltip text").not.toContain("[");
});
