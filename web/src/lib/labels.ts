/** Display labels come from the bundle's central dictionary (one source of
 * truth shared with the engine's HTML report). Raw codes never appear in
 * visible text - only in tooltips and technical-detail expanders. */

import { useBundle } from "./bundle";

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
  const bundle = useBundle();
  return (code: string) => {
    const hit = bundle.labels[code];
    return hit ?? { label: prettify(code), description: "" };
  };
}
