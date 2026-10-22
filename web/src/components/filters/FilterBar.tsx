/** Filter bar: labeled filters, active filters as removable chips, Clear all,
 * and the result count. All state lives in the URL via useFilterParams. */

import { ChipDescriptor } from "../../lib/filters";

export function FilterBar({
  children,
  chips,
  onClearAll,
  shown,
  total,
  noun,
}: {
  children: React.ReactNode;
  chips: ChipDescriptor[];
  onClearAll: () => void;
  shown: number;
  total: number;
  noun: string;
}) {
  return (
    <div className="mb-4 rounded-[8px] bg-inset/60 px-4 py-3">
      <div className="flex flex-wrap items-end gap-x-5 gap-y-3">{children}</div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-caption font-medium">
          Showing {shown.toLocaleString("en-US")} of {total.toLocaleString("en-US")} {noun}
        </span>
        {chips.map((c) => (
          <button
            key={c.key}
            onClick={c.onRemove}
            className="chip chip-outline cursor-pointer"
            aria-label={`Remove filter: ${c.label}`}
          >
            {c.label} {"×"}
          </button>
        ))}
        {chips.length > 0 && (
          <button
            onClick={onClearAll}
            className="cursor-pointer border-0 bg-transparent p-0 text-caption font-medium text-petrol underline-offset-4 hover:underline"
          >
            Clear all
          </button>
        )}
      </div>
    </div>
  );
}
