import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    // On CI, keep full debug evidence for any failure: the uploaded
    // test-results/ artifact then shows exactly what the page did.
    ...(process.env.CI
      ? {
          trace: "retain-on-failure" as const,
          screenshot: "only-on-failure" as const,
          video: "retain-on-failure" as const,
        }
      : {}),
  },
  webServer: {
    command: "npm run preview -- --host 127.0.0.1 --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
    timeout: 60_000,
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "mobile",
      use: { ...devices["Desktop Chrome"], viewport: { width: 375, height: 812 } },
    },
  ],
});
