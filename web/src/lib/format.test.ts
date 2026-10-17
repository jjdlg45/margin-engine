import { describe, expect, it } from "vitest";
import { count, dollars, dollarsShort, num, pct } from "./format";

describe("dollars", () => {
  it("renders cents as $x,xxx.xx", () => {
    expect(dollars(1142328403)).toBe("$11,423,284.03");
    expect(dollars(0)).toBe("$0.00");
  });
  it("signs negatives with a true minus", () => {
    expect(dollars(-1890000)).toBe("−$18,900.00");
  });
  it("em-dash for missing", () => {
    expect(dollars(null)).toBe("—");
    expect(dollars(undefined)).toBe("—");
  });
});

describe("dollarsShort", () => {
  it("abbreviates for axes", () => {
    expect(dollarsShort(124_000_000_00)).toBe("$124.00M");
    expect(dollarsShort(41_200_000)).toBe("$412k");
    expect(dollarsShort(-830_000)).toBe("−$8.3k");
    expect(dollarsShort(42_00)).toBe("$42");
  });
});

describe("pct / num / count", () => {
  it("formats shares and counts", () => {
    expect(pct(0.0712)).toBe("7.1%");
    expect(pct(null)).toBe("—");
    expect(num(-1.6234)).toBe("-1.62");
    expect(count(10661)).toBe("10,661");
  });
});
