/** Regenerate the README screenshots in docs/images/.
 *
 * Same mechanism as capture-review.mjs - vite preview plus headless chromium -
 * but a different job: capture-review.mjs writes the design-review folder, one
 * page in four themes; this writes the pictures the README walks a first-time
 * visitor through. Usage: npm run readme-shots
 * (builds are not triggered; run `npm run build` first if dist is stale).
 *
 * Targets come out of the published bundle rather than being hard-coded, so a
 * regenerated demo world cannot leave the README pointing at a pair or an
 * action that no longer exists.
 */

import { readFileSync, mkdirSync, statSync } from "node:fs";
import { spawn } from "node:child_process";
import { chromium } from "@playwright/test";

const PORT = 4191;
const OUT = "../docs/images";
const bundle = JSON.parse(readFileSync("public/data/bundle.json", "utf8"));

/** A pair whose current price sits below its reference band, so the band chart
 *  shows a gap worth looking at rather than a dot in the middle. */
const pair =
  bundle.guidance.find((g) => g.status === "below_reference" && g.band_p25 !== null) ??
  bundle.guidance.find((g) => g.band_p25 !== null);
/** An action far enough through the lifecycle that the tracker has steps behind
 *  it as well as ahead. */
const byStatus = new Map();
for (const a of bundle.actions_table ?? []) if (!byStatus.has(a.status)) byStatus.set(a.status, a);
const action =
  byStatus.get("implemented") ?? byStatus.get("exported") ?? byStatus.get("approved") ??
  (bundle.actions_table ?? [])[0];

if (!pair) throw new Error("no guidance pair with a band in the bundle");
if (!action) throw new Error("no actions in the bundle");

// `wait` is what has to exist before the shot is worth taking; `scrollTo`,
// where given, is what the picture is OF - several of these views put the
// thing worth showing below a viewport of headers and summary cards.
const SHOTS = [
  { name: "overview", path: "/", wait: "svg.recharts-surface" },
  {
    name: "guidance-pair",
    path: `/#/guidance?customer=${pair.customer_id}&item=${pair.item_id}`,
    wait: "svg",
    // Scroll to the figures UNDER the band chart, so the chart itself is
    // fully in frame; the first svg on the page is a header icon.
    scrollTo: 'dt:has-text("Pocket floor")',
  },
  { name: "waterfall", path: "/#/waterfall", wait: "svg.recharts-surface" },
  { name: "opportunity", path: "/#/opportunity", wait: "h1" },
  // The pass-through control is a custom slider, not an <input type=range>.
  { name: "scenarios", path: "/#/scenarios", wait: '[role="slider"], input[type="range"]' },
  {
    name: "action-center",
    path: "/#/actions",
    // The queue list is virtualized, so wait for a row rather than its
    // container: the container can exist before anything is in it.
    wait: 'a[href*="/actions/act_"]',
    scrollTo: 'a[href*="/actions/act_"]',
  },
  {
    name: "action-detail",
    path: `/#/actions/${action.action_id}`,
    wait: '[data-testid="step-tracker"]',
    scrollTo: '[data-testid="step-tracker"]',
  },
];

mkdirSync(OUT, { recursive: true });
const server = spawn(
  process.platform === "win32" ? "npx.cmd" : "npx",
  ["vite", "preview", "--host", "127.0.0.1", "--port", String(PORT), "--strictPort"],
  { stdio: "ignore", shell: process.platform === "win32" },
);
const stop = () => server.kill();
process.on("exit", stop);
await new Promise((r) => setTimeout(r, 4000));

const browser = await chromium.launch();

async function shot({ name, path, wait, scrollTo }, { theme, width, height, suffix }) {
  const page = await browser.newPage({
    viewport: { width, height },
    reducedMotion: "reduce",
  });
  await page.goto(`http://127.0.0.1:${PORT}${path}`);
  await page.evaluate((t) => {
    document.documentElement.dataset.theme = t;
  }, theme);
  try {
    await page.waitForSelector(wait, { timeout: 15000 });
  } catch {
    // A page without a chart still has a page worth showing; the per-file
    // size report below is what tells us a shot came out empty.
    console.warn(`  ${name}${suffix}: ${wait} never appeared, capturing anyway`);
  }
  if (scrollTo && !suffix) {
    await page
      .locator(scrollTo)
      .first()
      .scrollIntoViewIfNeeded()
      .catch(() => console.warn(`  ${name}: could not scroll to ${scrollTo}`));
    // Put a little of the page above it, so the shot does not look cropped.
    await page.evaluate(() => window.scrollBy(0, -140));
  }
  await page.waitForTimeout(600);
  const file = `${OUT}/${name}${suffix}.png`;
  await page.screenshot({ path: file });
  await page.close();
  const kb = Math.round(statSync(file).size / 1024);
  console.log(`saved ${name}${suffix}.png (${kb} KB)`);
  return kb;
}

// 1280x800 rather than the review folder's 1440x900: the README renders these
// at a column width, so the extra pixels are weight without detail.
const desktop = { theme: "light", width: 1280, height: 800, suffix: "" };
let total = 0;
for (const s of SHOTS) total += await shot(s, desktop);
total += await shot(SHOTS[0], { theme: "dark", width: 1280, height: 800, suffix: "-dark" });
total += await shot(SHOTS[0], { theme: "light", width: 390, height: 844, suffix: "-mobile" });

console.log(`\n${SHOTS.length + 2} images, ${total} KB total`);
await browser.close();
stop();
process.exit(0);
