/** Run 4 (26.1): the HTTP adapter builds requests exactly as the OpenAPI
 * contract declares, and the adapter selection follows VITE_DATA_SOURCE. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { tableQueryString } from "./httpAdapter";

describe("tableQueryString", () => {
  it("is empty for an empty query", () => {
    expect(tableQueryString({})).toBe("");
  });

  it("carries page, page_size and sort as declared", () => {
    expect(tableQueryString({ page: 2, page_size: 50, sort: "-gap_invoice_cents" })).toBe(
      "?page=2&page_size=50&sort=-gap_invoice_cents",
    );
  });

  it("URL-encodes filters as JSON and drops undefined/null entries", () => {
    const qs = tableQueryString({
      filters: {
        customer: "C10001",
        statuses: ["below_floor", "in_band"],
        pol_max: 90,
        gap_min: undefined,
        item: null,
      },
    });
    const params = new URLSearchParams(qs.slice(1));
    const filters = JSON.parse(params.get("filters")!);
    expect(filters).toEqual({
      customer: "C10001",
      statuses: ["below_floor", "in_band"],
      pol_max: 90,
    });
  });

  it("omits the filters parameter entirely when nothing is active", () => {
    expect(tableQueryString({ page: 1, filters: { customer: undefined } })).toBe("?page=1");
  });
});

describe("adapter selection", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("defaults to the static adapter", async () => {
    vi.resetModules();
    const mod = await import("./client");
    expect(mod.dataSource).toBe("static");
  });

  it("selects the http adapter only on VITE_DATA_SOURCE=http", async () => {
    vi.stubEnv("VITE_DATA_SOURCE", "http");
    vi.resetModules();
    const mod = await import("./client");
    expect(mod.dataSource).toBe("http");
  });
});
