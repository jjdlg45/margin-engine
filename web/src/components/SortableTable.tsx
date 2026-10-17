/** TanStack-backed table: sorting, optional global text filter, pagination.
 * Rendering only — every value arrives formatted or is formatted by the
 * caller's cell renderers; no financial math happens here. */

import {
  ColumnDef,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

export function SortableTable<T>({
  data,
  columns,
  filterPlaceholder,
  pageSize = 25,
  initialSort,
  filterUrlParam,
}: {
  data: T[];
  columns: ColumnDef<T, unknown>[];
  filterPlaceholder?: string;
  pageSize?: number;
  initialSort?: SortingState;
  /** When set, the filter text lives in this URL query param (shareable). */
  filterUrlParam?: string;
}) {
  const [sorting, setSorting] = useState<SortingState>(initialSort ?? []);
  const [params, setParams] = useSearchParams();
  const [localFilter, setLocalFilter] = useState("");
  const globalFilter = filterUrlParam ? (params.get(filterUrlParam) ?? "") : localFilter;
  const setGlobalFilter = (v: string) => {
    if (filterUrlParam) {
      setParams(
        (p) => {
          if (v) p.set(filterUrlParam, v);
          else p.delete(filterUrlParam);
          return p;
        },
        { replace: true },
      );
    } else {
      setLocalFilter(v);
    }
  };
  const table = useReactTable({
    data,
    columns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize } },
  });
  const pageCount = table.getPageCount();
  return (
    <div>
      {filterPlaceholder && (
        <div className="row mb-2">
          <input
            value={globalFilter}
            onChange={(e) => setGlobalFilter(e.target.value)}
            placeholder={filterPlaceholder}
            aria-label={filterPlaceholder}
          />
        </div>
      )}
      <div className="overflow-x-auto">
        <table>
          <thead>
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => {
                  const sorted = h.column.getIsSorted();
                  return (
                    <th key={h.id}>
                      {h.column.getCanSort() ? (
                        <button
                          className="w-full cursor-pointer border-0 bg-transparent p-0 text-center font-semibold text-ink/[0.87]"
                          onClick={h.column.getToggleSortingHandler()}
                          aria-label={`Sort by ${String(h.column.columnDef.header)}`}
                        >
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          {sorted === "asc" ? " ▴" : sorted === "desc" ? " ▾" : ""}
                        </button>
                      ) : (
                        flexRender(h.column.columnDef.header, h.getContext())
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id}>
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pageCount > 1 && (
        <div className="row mt-2 items-center">
          <button onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()}>
            Previous
          </button>
          <span className="text-caption secondary">
            Page {table.getState().pagination.pageIndex + 1} of {pageCount}
          </span>
          <button onClick={() => table.nextPage()} disabled={!table.getCanNextPage()}>
            Next
          </button>
        </div>
      )}
    </div>
  );
}
