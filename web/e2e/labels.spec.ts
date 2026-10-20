/** Phase 23.5 gate: no raw machine codes in visible text on any page.
 * Scans rendered innerText for snake_case tokens and hyphenated flag codes.
 * Tooltips and collapsed technical-detail expanders are not visible text, so
 * raw codes may live there. The allowlist names intentional technical terms
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
    const text = await page.evaluate(() => document.body.innerText);
    const bad = violations(text);
    expect(bad, `raw codes visible on ${route}: ${bad.join(", ")}`).toHaveLength(0);
  });
}
