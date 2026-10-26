/** Load your data (polish round), API mode only: upload a real export, review
 * the profiler's suggestions, confirm each with clicks, save the validated
 * mapping, run the pipeline and open the results. Runs LAST in this suite
 * (alphabetical, serial) because the finished run swaps the served bundle;
 * the server reloads the committed bundle at its next start. */

import { expect, test, type Page } from "@playwright/test";

const SAMPLE = "../data/sample/erp/invoice_lines.csv";

async function uploadAndProfile(page: Page) {
  await page.goto("/#/load");
  await expect(page.getByRole("heading", { level: 1, name: "Load your data" })).toBeVisible();
  await page.getByTestId("load-file").setInputFiles(SAMPLE);
  await expect(page.getByTestId("confirmables")).toBeVisible({ timeout: 30_000 });
  // Repoint the transaction-code column to the true one; the code list follows.
  await page.getByRole("button", { name: "TXN_CD", exact: true }).click();
  await expect(page.getByText('Transaction code "S" means')).toBeVisible({ timeout: 30_000 });
}

async function pickColumn(page: Page, label: string, column: string) {
  const combo = page.getByRole("combobox", { name: label });
  await combo.click();
  await combo.fill(column);
  await page.getByRole("option", { name: column, exact: true }).click();
}

test("the load page exists in API mode navigation", async ({ page }) => {
  await page.goto("/#/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.getByRole("link", { name: "Load your data", exact: true })).toBeVisible();
});

test("a wrong confirmation fails with the engine's plain-English message", async ({ page }) => {
  test.setTimeout(180_000);
  await uploadAndProfile(page);
  // Leave the undetected invoice-date column as FIXME_UNKNOWN: the mapping
  // saves (schema-valid) and the RUN fails fast with the loader's message.
  await page.getByTestId("save-mapping").click();
  await expect(page.getByTestId("mapping-saved")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("run-pipeline").click();
  await expect(page.getByTestId("run-failed")).toBeVisible({ timeout: 120_000 });
  const message = await page.getByTestId("run-failed").textContent();
  expect(message).toContain("not found in the file");
  expect(message).not.toContain("Traceback");
});

test("upload, confirm with clicks, run, open the results", async ({ page }) => {
  test.setTimeout(420_000);
  await uploadAndProfile(page);
  // Fix what the profiler could not guess, exactly as a person would.
  await pickColumn(page, "Column for invoice date column", "INV_DT");
  await page.getByLabel("Invoice date format").fill("%m/%d/%Y");
  await pickColumn(page, "Column for net amount column", "EXT_NET");
  for (const [code, meaning] of [
    ["R", "return"],
    ["C", "credit"],
    ["F", "free_good"],
  ] as const) {
    await page
      .locator("li", { hasText: `Transaction code "${code}" means` })
      .getByRole("button", { name: meaning, exact: true })
      .click();
  }
  await page.getByTestId("save-mapping").click();
  await expect(page.getByTestId("mapping-saved")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("run-pipeline").click();
  await expect(page.getByTestId("run-done")).toBeVisible({ timeout: 360_000 });
  await expect(page.getByTestId("run-done")).toContainText("The site now serves your data");
  await page.getByTestId("open-results").click();
  await expect(page).toHaveURL(/#\/$/);
  await expect(page.getByTestId("data-notice")).toContainText(
    "Your data: invoice_lines.csv (local run)",
    { timeout: 60_000 },
  );
});
