/** Display labels come from the app payload's central dictionary (one source
 * of truth shared with the engine's HTML report). Raw codes never appear in
 * visible text - only in tooltips and technical-detail expanders. */

import { useAppData } from "./queries";

export interface LabelEntry {
  label: string;
  description: string;
}

/** Fallback for codes missing from the dictionary: prettify, never leak. */
export function prettify(code: string): string {
  const s = code.replace(/[_-]/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function useLabels(): (code: string) => LabelEntry {
  const app = useAppData();
  const labels = app.data?.labels;
  return (code: string) => labels?.[code] ?? { label: prettify(code), description: "" };
}
