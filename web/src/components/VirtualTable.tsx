/** Virtualized table (TanStack Virtual): only the visible rows exist in the
 * DOM, with spacer rows preserving scroll geometry, so tables stay fast at
 * thousands of rows. Rendering only - no data math. */

import { useVirtualizer } from "@tanstack/react-virtual";
import { ReactNode, useRef } from "react";

export function VirtualTable<T>({
  rows,
  head,
  renderRow,
  label,
  estimateSize = 44,
  maxHeight = 560,
}: {
  rows: T[];
  head: ReactNode;
  renderRow: (row: T, index: number) => ReactNode;
  label: string;
  estimateSize?: number;
  maxHeight?: number;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize,
    overscan: 8,
  });
  const items = virtualizer.getVirtualItems();
  const padTop = items.length ? items[0].start : 0;
  const padBottom = items.length
    ? virtualizer.getTotalSize() - items[items.length - 1].end
    : 0;
  return (
    <div
      ref={parentRef}
      role="region"
      aria-label={label}
      tabIndex={0}
      data-virtualized="true"
      className="overflow-auto"
      style={{ maxHeight }}
    >
      <table>
        <thead className="sticky top-0 z-10 bg-paper">{head}</thead>
        <tbody>
          {padTop > 0 && (
            <tr aria-hidden="true" style={{ height: padTop }}>
              <td colSpan={99} className="border-0 p-0" />
            </tr>
          )}
          {items.map((it) => (
            <tr
              key={it.key}
              data-index={it.index}
              ref={virtualizer.measureElement}
            >
              {renderRow(rows[it.index], it.index)}
            </tr>
          ))}
          {padBottom > 0 && (
            <tr aria-hidden="true" style={{ height: padBottom }}>
              <td colSpan={99} className="border-0 p-0" />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
