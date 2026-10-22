/** Shared filter state: every filter lives in the URL (shareable links).
 * Pages declare their filters; this hook reads/writes params and produces the
 * removable-chip descriptors for the FilterBar. Filtering itself happens in
 * the page over bundle rows - pure selection, never recomputation. */

import { useSearchParams } from "react-router-dom";

export interface ChipDescriptor {
  key: string;
  label: string;
  onRemove: () => void;
}

export interface Range {
  min: number | null;
  max: number | null;
}

export function useFilterParams() {
  const [params, setParams] = useSearchParams();

  const set = (key: string, value: string | null) =>
    setParams(
      (p) => {
        if (value) p.set(key, value);
        else p.delete(key);
        return p;
      },
      { replace: true },
    );

  const getList = (key: string): string[] => {
    const raw = params.get(key);
    return raw ? raw.split("~").filter(Boolean) : [];
  };
  const setList = (key: string, values: string[]) => set(key, values.join("~") || null);

  const getRange = (key: string): Range => {
    const raw = params.get(key);
    if (!raw) return { min: null, max: null };
    const [lo, hi] = raw.split("~");
    return {
      min: lo !== "" && lo !== undefined ? Number(lo) : null,
      max: hi !== "" && hi !== undefined ? Number(hi) : null,
    };
  };
  const setRange = (key: string, r: Range) => {
    if (r.min === null && r.max === null) set(key, null);
    else set(key, `${r.min ?? ""}~${r.max ?? ""}`);
  };

  const clearAll = (keys: string[]) =>
    setParams(
      (p) => {
        for (const k of keys) p.delete(k);
        return p;
      },
      { replace: true },
    );

  return { params, set, getList, setList, getRange, setRange, clearAll };
}

export function inRange(value: number | null | undefined, r: Range): boolean {
  if (value === null || value === undefined || !isFinite(value)) {
    return r.min === null && r.max === null;
  }
  if (r.min !== null && value < r.min) return false;
  if (r.max !== null && value > r.max) return false;
  return true;
}

export function rangeChipLabel(label: string, r: Range, unit = ""): string | null {
  if (r.min === null && r.max === null) return null;
  if (r.min !== null && r.max !== null) return `${label}: ${r.min}–${r.max}${unit}`;
  if (r.min !== null) return `${label}: ≥ ${r.min}${unit}`;
  return `${label}: ≤ ${r.max}${unit}`;
}
