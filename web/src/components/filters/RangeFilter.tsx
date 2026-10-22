/** Min/max range filter with optional presets, on React Aria NumberFields. */

import { Input, Label, NumberField } from "react-aria-components";
import { Range } from "../../lib/filters";

export function RangeFilter({
  label,
  range,
  onChange,
  unit,
  presets,
  step,
}: {
  label: string;
  range: Range;
  onChange: (r: Range) => void;
  unit?: string;
  presets?: { label: string; range: Range }[];
  step?: number;
}) {
  const field = (which: "min" | "max") => (
    <NumberField
      aria-label={`${label} ${which}`}
      value={range[which] ?? NaN}
      step={step}
      onChange={(v) => onChange({ ...range, [which]: Number.isNaN(v) ? null : v })}
      className="w-24"
    >
      <Input
        placeholder={which}
        className="w-full rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/[0.87]"
      />
    </NumberField>
  );
  return (
    <div className="flex flex-col gap-1">
      <Label className="text-caption secondary">
        {label}
        {unit ? ` (${unit})` : ""}
      </Label>
      <div className="flex items-center gap-1">
        {field("min")}
        <span className="secondary">{"–"}</span>
        {field("max")}
      </div>
      {presets && (
        <div className="flex flex-wrap gap-1">
          {presets.map((p) => (
            <button
              key={p.label}
              type="button"
              onClick={() => onChange(p.range)}
              className="rounded-badge border hairline bg-card px-1.5 py-0.5 text-caption secondary hover:text-ink"
            >
              {p.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
