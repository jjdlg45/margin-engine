/** Formatting only — the UI never recalculates financials. Money arrives as
 * integer cents from the bundle and is rendered here. */

export function dollars(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "—";
  const sign = cents < 0 ? "−" : "";
  return `${sign}$${(Math.abs(cents) / 100).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Axis-style abbreviation: $1.24M, $412k, −$8.3k. Exact values stay in
 * tooltips and tables. */
export function dollarsShort(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "—";
  const sign = cents < 0 ? "−" : "";
  const d = Math.abs(cents) / 100;
  if (d >= 1_000_000) return `${sign}$${(d / 1_000_000).toFixed(2)}M`;
  if (d >= 10_000) return `${sign}$${Math.round(d / 1_000)}k`;
  if (d >= 1_000) return `${sign}$${(d / 1_000).toFixed(1)}k`;
  return `${sign}$${d.toFixed(0)}`;
}

export function pct(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined || !isFinite(x)) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}

export function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || !isFinite(x)) return "—";
  return x.toFixed(digits);
}

export function count(x: number | null | undefined): string {
  if (x === null || x === undefined) return "—";
  return x.toLocaleString("en-US");
}

/** Price index (1.0 = list) as a percent-of-list figure: 0.845 -> "84.5%". */
export function pctOfList(x: number | null | undefined): string {
  if (x === null || x === undefined || !isFinite(x)) return "—";
  return `${(x * 100).toFixed(1)}%`;
}

/** Signed percentage for margins: -0.007 -> "−0.7%". */
export function signedPct(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined || !isFinite(x)) return "—";
  const sign = x < 0 ? "−" : "";
  return `${sign}${(Math.abs(x) * 100).toFixed(digits)}%`;
}
