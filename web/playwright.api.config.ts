/** API-mode e2e (Run 4, 30.5/30.7): the UI runs with VITE_DATA_SOURCE=http
 * against the real BFF (api/), /api proxied same-origin by the vite dev
 * server. Run with: npx playwright test -c playwright.api.config.ts */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e-api",
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4174",
  },
  webServer: [
    {
      command:
        "cd .. && uv run uvicorn api.main:app --host 127.0.0.1 --port 8321",
      url: "http://127.0.0.1:8321/api/health",
      reuseExistingServer: true,
      timeout: 120_000,
      env: {
        // Seed the action store from the exported bundle dir (Phase 42); a
        // dedicated DB keeps review-state churn out of any real local state.
        // The reset makes each suite run hermetic: lifecycle tests consume
        // proposed actions (flagged ones are scarce), so a persistent store
        // exhausts them across repeated local runs. The server deletes the
        // file at ITS OWN start (never mid-run; Playwright launches web
        // servers before globalSetup, so a setup-script delete lands on a
        // live store). A reused already-running server keeps its state.
        MARGIN_API_ACTIONS_SEED: "web/public/data",
        MARGIN_API_ACTIONS_DB: ".api_state/e2e-actions.duckdb",
        MARGIN_API_ACTIONS_RESET: "1",
        // The load-your-data suite uploads files and swaps the served bundle;
        // dedicated paths keep that churn out of any real local state. The
        // bundle always reloads from web/public/data at server start, so the
        // next run is hermetic regardless.
        MARGIN_API_DB: ".api_state/e2e-api.duckdb",
        MARGIN_API_UPLOADS_DIR: ".api_state/e2e-uploads",
      },
    },
    {
      command:
        "npm run dev -- --host 127.0.0.1 --port 4174 --strictPort",
      url: "http://127.0.0.1:4174",
      reuseExistingServer: true,
      timeout: 120_000,
      env: {
        VITE_DATA_SOURCE: "http",
        VITE_API_PROXY_TARGET: "http://127.0.0.1:8321",
      },
    },
  ],
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
  ],
});
