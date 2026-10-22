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
