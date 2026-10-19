/** Regenerate the committed review screenshots in docs/ui/review/.
 * The only writer of that folder - normal Playwright runs write to the
 * ignored web/screenshots/ instead. Usage: npm run review-shots
 * (builds are not triggered; run `npm run build` first if dist is stale). */

import { spawn } from "node:child_process";
import { chromium } from "@playwright/test";

const PORT = 4190;
const server = spawn(
  process.platform === "win32" ? "npx.cmd" : "npx",
  ["vite", "preview", "--host", "127.0.0.1", "--port", String(PORT), "--strictPort"],
  { stdio: "ignore", shell: process.platform === "win32" },
);
const stop = () => server.kill();
process.on("exit", stop);

await new Promise((r) => setTimeout(r, 4000));
const browser = await chromium.launch();

for (const [w, h, device] of [
  [1440, 900, "desktop"],
  [375, 812, "mobile"],
]) {
  for (const theme of ["light", "dark"]) {
    const page = await browser.newPage({
      viewport: { width: w, height: h },
      reducedMotion: "reduce",
    });
    await page.goto(`http://127.0.0.1:${PORT}/`);
    await page.evaluate((t) => {
      document.documentElement.dataset.theme = t;
    }, theme);
    await page.waitForSelector("svg.recharts-surface");
    await page.waitForTimeout(400);
    await page.screenshot({
      path: `../docs/ui/review/overview-${theme}-${device}.png`,
      fullPage: true,
    });
    if (device === "desktop") {
      await page
        .locator('section[aria-labelledby="wf"]')
        .screenshot({ path: `../docs/ui/review/waterfall-${theme}-desktop-after.png` });
    }
    await page.close();
    console.log(`saved overview-${theme}-${device}.png`);
  }
}
await browser.close();
stop();
process.exit(0);
