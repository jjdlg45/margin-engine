/** Phase 22 gate: Lighthouse desktop scores against the preview build.
 * Fails (exit 1) below performance 90, accessibility 95, best practices 95.
 * Run: node lighthouse.mjs (a compression-enabled static server on
 * 127.0.0.1:5050 - `npx serve@14.2.4 -l 5050 dist` - mirrors GitHub Pages). */

import { execFileSync } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";

const THRESHOLDS = { performance: 0.9, accessibility: 0.95, "best-practices": 0.95 };
const out = "lighthouse-report.json";

execFileSync(
  "npx",
  [
    "--yes",
    "lighthouse@12.2.1",
    "http://127.0.0.1:5050/",
    "--preset=desktop",
    "--only-categories=performance,accessibility,best-practices",
    `--output-path=${out}`,
    "--output=json",
    "--quiet",
    '--chrome-flags=--headless=new',
  ],
  { stdio: "inherit", shell: process.platform === "win32" },
);

const report = JSON.parse(readFileSync(out, "utf-8"));
let failed = false;
for (const [cat, min] of Object.entries(THRESHOLDS)) {
  const score = report.categories[cat]?.score ?? 0;
  const ok = score >= min;
  console.log(`${cat}: ${(score * 100).toFixed(0)} (gate ${min * 100}) ${ok ? "PASS" : "FAIL"}`);
  if (!ok) failed = true;
}
// The individual metrics, so a failing score is diagnosable from the log.
const METRICS = [
  ["largest-contentful-paint", "LCP"],
  ["total-blocking-time", "TBT"],
  ["cumulative-layout-shift", "CLS"],
  ["first-contentful-paint", "FCP"],
  ["speed-index", "Speed Index"],
];
for (const [id, name] of METRICS) {
  const a = report.audits[id];
  if (a) console.log(`${name}: ${a.displayValue ?? a.numericValue} (score ${a.score})`);
}
// Keep the full report for the CI artifact when the gate fails.
if (failed) console.log(`Gate failed - full report kept at ${out}`);
else rmSync(out, { force: true });
process.exit(failed ? 1 : 0);
