/**
 * Refuse to run the browser suite against a stale build.
 *
 * `npm run preview` serves `dist/`, so the suite tests whatever was built
 * last, not the working tree. That is how a change to ScenariosPage once
 * passed every full-page snapshot it should have failed: the page under test
 * was a build from before the change, and a green run proved nothing.
 *
 * The webServer command builds before starting, which covers the normal
 * case. It does NOT cover `reuseExistingServer`: if a preview server is
 * already up - left over from an earlier run, or started by hand - Playwright
 * skips the command entirely and never builds. This guard is for that case.
 * It is a globalSetup, so it runs whichever way the server got there.
 */

import { execSync } from "node:child_process";
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
// Everything the built site is made of. public/data is included because the
// bundle is engine output: re-exporting it and forgetting to rebuild shows
// the old numbers, which is the same failure wearing different clothes.
const SOURCES = ["src", "public", "index.html", "vite.config.ts", "package.json"];
const BUILD = "dist";

function newestMtime(path: string): number {
  let stat;
  try {
    stat = statSync(path);
  } catch {
    return 0;
  }
  if (!stat.isDirectory()) return stat.mtimeMs;
  let newest = 0;
  for (const entry of readdirSync(path)) {
    newest = Math.max(newest, newestMtime(join(path, entry)));
  }
  return newest;
}

export default function assertBuildIsCurrent(): void {
  const builtAt = newestMtime(join(ROOT, BUILD));
  if (builtAt === 0) {
    throw new Error(
      "web/dist does not exist, so there is nothing to test.\n" +
        "Run:  npm run build\n",
    );
  }
  const sourceAt = Math.max(...SOURCES.map((s) => newestMtime(join(ROOT, s))));
  if (sourceAt <= builtAt) return;

  // Name the newest offending file: "something is stale" sends people
  // hunting, "this file is newer than the build" does not.
  let culprit = "";
  const walk = (p: string): void => {
    let stat;
    try {
      stat = statSync(p);
    } catch {
      return;
    }
    if (!stat.isDirectory()) {
      if (stat.mtimeMs === sourceAt) culprit = p.slice(ROOT.length).replace(/\\/g, "/");
      return;
    }
    for (const e of readdirSync(p)) walk(join(p, e));
  };
  for (const s of SOURCES) walk(join(ROOT, s));

  const age = Math.round((sourceAt - builtAt) / 1000);
  throw new Error(
    `web/dist is older than the source, so this run would test the PREVIOUS build.\n` +
      `  newest source: ${culprit || "(unknown)"}  (${age}s newer than dist)\n` +
      `\n` +
      `Snapshots compared against a stale build are worthless - a change can\n` +
      `pass every visual check without ever being rendered.\n` +
      `\n` +
      `Run:  npm run build\n` +
      `If the engine exports changed too:  uv run margin-engine export-web --demo\n`,
  );
}

// Also usable directly: `node --experimental-strip-types e2e/assert-build-is-current.ts`
if (process.argv[1]?.endsWith("assert-build-is-current.ts")) {
  try {
    assertBuildIsCurrent();
    execSync("echo build is current", { stdio: "inherit" });
  } catch (err) {
    console.error((err as Error).message);
    process.exit(1);
  }
}
