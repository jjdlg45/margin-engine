/** Opportunity: the three levels side by side, visually separated, never
 * summed; top opportunities with explanation and evidence status. Rows come
 * through the data client's filterable table endpoint; sorting and paging of
 * the filtered set happen in the table component. */

import { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";
import { QuerySection } from "../components/Async";
import { EntityLabel, entityText } from "../components/EntityLabel";
import { EntityCombobox } from "../components/filters/Combobox";
import { FilterBar } from "../components/filters/FilterBar";
import { MultiSelect } from "../components/filters/MultiSelect";
import { RangeFilter } from "../components/filters/RangeFilter";
import type { OpportunityData } from "../lib/api/types";
import { ChipDescriptor, rangeChipLabel, useFilterParams } from "../lib/filters";
import { useLabels } from "../lib/labels";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { SortableTable } from "../components/SortableTable";
import { StatusChip } from "../components/StatusChip";
import { usePageData, useTableQuery } from "../lib/queries";
import { count, dollars, dollarsShort } from "../lib/format";
import type { OpportunityRecord } from "../types/bundle";

const FILTER_KEYS = ["customer", "item", "evidence", "treatment", "gross", "below"];

function OpportunityTable({ facets }: { facets: OpportunityData["facets"] }) {
  const labels = useLabels();
  const f = useFilterParams();
  const customer = f.params.get("customer");
  const item = f.params.get("item");
  const evidences = f.getList("evidence");
  const treatments = f.getList("treatment");
  const grossRange = f.getRange("gross"); // dollars
  const belowRange = f.getRange("below"); // percent below target

  const table = useTableQuery("opportunities", {
    page: 1,
    page_size: 5000,
    filters: {
      customer: customer ?? undefined,
      item: item ?? undefined,
      evidence: evidences.length ? evidences : undefined,
      treatments: treatments.length ? treatments : undefined,
      gross_min: grossRange.min ?? undefined,
      gross_max: grossRange.max ?? undefined,
      below_min: belowRange.min ?? undefined,
      below_max: belowRange.max ?? undefined,
    },
  });

  const chips: ChipDescriptor[] = [];
  if (customer)
    chips.push({
      key: "customer",
      label: entityText(facets.customers.find((c) => c.id === customer)?.name, customer),
      onRemove: () => f.set("customer", null),
    });
  if (item)
    chips.push({
      key: "item",
      label: entityText(facets.items.find((i) => i.id === item)?.name, item),
      onRemove: () => f.set("item", null),
    });
  for (const e of evidences)
    chips.push({
      key: `e-${e}`,
      label: labels(e).label,
      onRemove: () => f.setList("evidence", evidences.filter((x) => x !== e)),
    });
  for (const t of treatments)
    chips.push({
      key: `t-${t}`,
      label: labels(t).label,
      onRemove: () => f.setList("treatment", treatments.filter((x) => x !== t)),
    });
  const grossChip = rangeChipLabel("Gross", grossRange, " $");
  if (grossChip) chips.push({ key: "gross", label: grossChip, onRemove: () => f.setRange("gross", { min: null, max: null }) });
  const belowChip = rangeChipLabel("Below target", belowRange, "%");
  if (belowChip) chips.push({ key: "below", label: belowChip, onRemove: () => f.setRange("below", { min: null, max: null }) });

  const columns = useMemo<ColumnDef<OpportunityRecord, unknown>[]>(
    () => [
      {
        header: "Customer",
        accessorKey: "customer_id",
        meta: { left: true },
        cell: (c) => (
          <span className="left block">
            <EntityLabel name={c.row.original.customer_name} id={c.row.original.customer_id} />
          </span>
        ),
      },
      {
        header: "Item",
        accessorKey: "item_id",
        meta: { left: true },
        cell: (c) => (
          <span className="left block">
            <EntityLabel name={c.row.original.item_name} id={c.row.original.item_id} />
          </span>
        ),
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
        cell: (c) => <StatusChip status={c.row.original.volume_treatment} />,
      },
      {
        header: "Evidence",
        accessorKey: "evidence_status",
        cell: (c) => <StatusChip status={c.row.original.evidence_status ?? "insufficient_data"} />,
      },
      {
        header: "Why",
        accessorKey: "summary",
        enableSorting: false,
        meta: { left: true },
        cell: (c) => (
          <span className="why min-w-[20rem] text-caption">
            {c.row.original.summary}
            <details className="mt-1 secondary">
              <summary>Full explanation</summary>
              {c.row.original.explanation}
            </details>
          </span>
        ),
      },
    ],
    [],
  );

  return (
    <>
      {/* Filter controls stay mounted across result refetches (see
          GuidancePage); only the table swaps to a skeleton. */}
      <FilterBar
        chips={chips}
        onClearAll={() => f.clearAll(FILTER_KEYS)}
        shown={table.data?.total}
        total={table.data?.total_unfiltered}
        noun="opportunities"
      >
        <EntityCombobox label="Customer" options={facets.customers} value={customer} onChange={(v) => f.set("customer", v)} />
        <EntityCombobox label="Item" options={facets.items} value={item} onChange={(v) => f.set("item", v)} />
        <MultiSelect
          label="Evidence"
          options={facets.evidence}
          values={evidences}
          onChange={(v) => f.setList("evidence", v)}
          renderOption={(e) => labels(e).label}
        />
        <MultiSelect
          label="Volume treatment"
          options={facets.treatments}
          values={treatments}
          onChange={(v) => f.setList("treatment", v)}
          renderOption={(t) => labels(t).label}
        />
        <RangeFilter label="Gross opportunity" unit="$" range={grossRange} onChange={(r) => f.setRange("gross", r)} />
        <RangeFilter
          label="Below target"
          unit="%"
          range={belowRange}
          onChange={(r) => f.setRange("below", r)}
          presets={[{ label: "More than 10% below", range: { min: 10, max: null } }]}
        />
      </FilterBar>
      <QuerySection query={table} label="top opportunities" lines={8}>
        {(page) =>
          page.items.length ? (
            <SortableTable
              data={page.items}
              columns={columns}
              pageSize={10}
              initialSort={[{ id: "gross_price_opportunity_cents", desc: true }]}
            />
          ) : (
            <EmptyState title="No eligible opportunities">
              Every pair is either contract-protected or lacks the data guidance
              needs, so the engine sizes nothing rather than guessing.
            </EmptyState>
          )
        }
      </QuerySection>
    </>
  );
}

export function OpportunityPage() {
  const page = usePageData("opportunity");
  return (
    <div>
      <QuerySection query={page} label="opportunity levels" lines={8}>
        {(o) => (
          <>
            <PageHeader title="Opportunity" lede={o.never_summed_note} />
            <div className="mt-2 grid gap-4 sm:grid-cols-3">
              <div className="panel px-5 py-5">
                <h2 className="m-0 text-section">Observed leakage</h2>
                <p className="m-0 mt-1 text-title font-semibold text-petrol">
                  {dollarsShort(o.leakage_total_cents)}
                </p>
                <p className="m-0 text-caption secondary">{o.window_label}</p>
                <p className="mb-0 mt-2 text-caption secondary">
                  Reconciled dollars that left between list and pocket - an
                  accounting fact, not a recommendation.
                </p>
              </div>
              <div className="panel px-5 py-5">
                <h2 className="m-0 text-section">Gross price opportunity</h2>
                <p className="m-0 mt-1 text-title font-semibold text-petrol">
                  {dollarsShort(o.gross_total_cents)}
                </p>
                <p className="m-0 text-caption secondary">{o.window_label}, at unchanged volume</p>
                <p className="mb-0 mt-2 text-caption secondary">
                  Eligible quantity times the positive pocket gap to the proposed
                  price, at unchanged volume - a ceiling, not a forecast.
                  {o.gross_conservative_total_cents !== null && (
                    <>
                      {" "}Conservative variant (below-P25 pairs raised to P25
                      only): {dollarsShort(o.gross_conservative_total_cents)}.
                    </>
                  )}
                </p>
              </div>
              <div className="panel px-5 py-5">
                <h2 className="m-0 text-section">Expected contribution change</h2>
                <p className="m-0 mt-1 text-title font-semibold text-petrol">
                  {dollarsShort(o.expected_total_cents)}
                </p>
                <p className="m-0 text-caption secondary">{o.window_label}, causal-supported records only</p>
                <p className="mb-0 mt-2 text-caption secondary">
                  {o.expected_total_note}
                  {o.n_outside_price_support > 0 && (
                    <>
                      {" "}{count(o.n_outside_price_support)} records propose moves outside
                      the segment's identified price range and are sized at
                      unchanged volume instead.
                    </>
                  )}
                  {o.n_cost_unavailable > 0 && (
                    <>
                      {" "}{count(o.n_cost_unavailable)} records have no known unit cost and
                      are excluded from cost-dependent levels.
                    </>
                  )}
                </p>
              </div>
            </div>
            <Section
              title="Top opportunities"
              lede={`${count(o.n_records)} records in this run; the table shows the largest by gross opportunity, each with the engine's explanation and evidence status.`}
            >
              <OpportunityTable facets={o.facets} />
            </Section>
          </>
        )}
      </QuerySection>
    </div>
  );
}
