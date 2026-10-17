/** Opportunity: the three levels side by side, visually separated, never
 * summed; top opportunities with explanation and evidence status. */

import { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { SortableTable } from "../components/SortableTable";
import { StatusChip } from "../components/StatusChip";
import { useBundle } from "../lib/bundle";
import { count, dollars, dollarsShort } from "../lib/format";
import type { OpportunityRecord } from "../types/bundle";

export function OpportunityPage() {
  const b = useBundle();
  const o = b.opportunity;
  const columns = useMemo<ColumnDef<OpportunityRecord, unknown>[]>(
    () => [
      {
        header: "Customer",
        accessorKey: "customer_id",
        cell: (c) => <span className="font-mono">{c.row.original.customer_id}</span>,
      },
      {
        header: "Item",
        accessorKey: "item_id",
        cell: (c) => <span className="font-mono">{c.row.original.item_id}</span>,
      },
      {
        header: "Gross opportunity",
        accessorKey: "gross_price_opportunity_cents",
        cell: (c) => dollars(c.row.original.gross_price_opportunity_cents),
      },
      {
        header: "Expected change",
        accessorKey: "expected_contribution_change_cents",
        cell: (c) =>
          c.row.original.expected_contribution_change_cents !== null
            ? dollars(c.row.original.expected_contribution_change_cents)
            : "—",
      },
      {
        header: "Volume treatment",
        accessorKey: "volume_treatment",
      },
      {
        header: "Evidence",
        accessorKey: "evidence_status",
        cell: (c) => <StatusChip status={c.row.original.evidence_status ?? "insufficient_data"} />,
      },
      {
        header: "Why",
        accessorKey: "explanation",
        enableSorting: false,
        cell: (c) => (
          <span className="left block max-w-md text-caption">{c.row.original.explanation}</span>
        ),
      },
    ],
    [],
  );
  return (
    <div>
      <PageHeader title="Opportunity" lede={o.never_summed_note} />
      <div className="mt-2 grid gap-4 sm:grid-cols-3">
        <div className="panel px-5 py-5">
          <p className="m-0 text-caption secondary">Level 1 · observed leakage ({o.window_label})</p>
          <p className="m-0 mt-1 text-title font-semibold text-petrol">
            {dollarsShort(o.leakage_total_cents)}
          </p>
          <p className="mb-0 mt-2 text-caption secondary">
            Reconciled dollars that left between list and pocket - an
            accounting fact, not a recommendation.
          </p>
        </div>
        <div className="panel px-5 py-5">
          <p className="m-0 text-caption secondary">Level 2 · gross price opportunity</p>
          <p className="m-0 mt-1 text-title font-semibold text-petrol">
            {dollarsShort(o.gross_total_cents)}
          </p>
          <p className="mb-0 mt-2 text-caption secondary">
            Eligible quantity times the positive gap to the proposed pocket
            price, at unchanged volume - a ceiling, not a forecast.
          </p>
        </div>
        <div className="panel px-5 py-5">
          <p className="m-0 text-caption secondary">Level 3 · expected contribution change</p>
          <p className="m-0 mt-1 text-title font-semibold text-petrol">
            {dollarsShort(o.expected_total_cents)}
          </p>
          <p className="mb-0 mt-2 text-caption secondary">{o.expected_total_note}</p>
        </div>
      </div>
      <Section
        title="Top opportunities"
        lede={`${count(o.n_records)} records in this run; the table shows the largest by gross opportunity, each with the engine's explanation and evidence status.`}
      >
        {o.top_opportunities.length ? (
          <SortableTable
            data={o.top_opportunities}
            columns={columns}
            filterPlaceholder="Filter by customer or item"
            filterUrlParam="filter"
            initialSort={[{ id: "gross_price_opportunity_cents", desc: true }]}
          />
        ) : (
          <EmptyState title="No eligible opportunities">
            Every pair is either contract-protected or lacks the data guidance
            needs, so the engine sizes nothing rather than guessing.
          </EmptyState>
        )}
      </Section>
    </div>
  );
}
