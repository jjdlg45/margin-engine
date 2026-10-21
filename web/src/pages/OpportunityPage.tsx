/** Opportunity: the three levels side by side, visually separated, never
 * summed; top opportunities with explanation and evidence status. */

import { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";
import { EntityLabel, entityText } from "../components/EntityLabel";
import { EntityCombobox } from "../components/filters/Combobox";
import { FilterBar } from "../components/filters/FilterBar";
import { MultiSelect } from "../components/filters/MultiSelect";
import { RangeFilter } from "../components/filters/RangeFilter";
import { ChipDescriptor, inRange, rangeChipLabel, useFilterParams } from "../lib/filters";
import { useLabels } from "../lib/labels";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { SortableTable } from "../components/SortableTable";
import { StatusChip } from "../components/StatusChip";
import { useBundle } from "../lib/bundle";
import { count, dollars, dollarsShort } from "../lib/format";
import type { OpportunityRecord } from "../types/bundle";

const FILTER_KEYS = ["customer", "item", "evidence", "treatment", "gross", "below"];

export function OpportunityPage() {
  const b = useBundle();
  const o = b.opportunity;
  const labels = useLabels();
  const f = useFilterParams();
  const customer = f.params.get("customer");
  const item = f.params.get("item");
  const evidences = f.getList("evidence");
  const treatments = f.getList("treatment");
  const grossRange = f.getRange("gross"); // dollars
  const belowRange = f.getRange("below"); // percent below target

  const customers = useMemo(() => {
    const seen = new Map<string, string | null>();
    for (const r of o.top_opportunities)
      if (!seen.has(r.customer_id)) seen.set(r.customer_id, r.customer_name ?? null);
    return [...seen].map(([id, name]) => ({ id, name }));
  }, [o.top_opportunities]);
  const itemOpts = useMemo(() => {
    const seen = new Map<string, string | null>();
    for (const r of o.top_opportunities)
      if ((!customer || r.customer_id === customer) && !seen.has(r.item_id))
        seen.set(r.item_id, r.item_name ?? null);
    return [...seen].map(([id, name]) => ({ id, name }));
  }, [o.top_opportunities, customer]);
  const evidenceOpts = useMemo(
    () => [...new Set(o.top_opportunities.map((r) => r.evidence_status ?? "insufficient_data"))].sort(),
    [o.top_opportunities],
  );
  const treatmentOpts = useMemo(
    () => [...new Set(o.top_opportunities.map((r) => r.volume_treatment))].sort(),
    [o.top_opportunities],
  );

  const filtered = o.top_opportunities.filter(
    (r) =>
      (!customer || r.customer_id === customer) &&
      (!item || r.item_id === item) &&
      (!evidences.length || evidences.includes(r.evidence_status ?? "insufficient_data")) &&
      (!treatments.length || treatments.includes(r.volume_treatment)) &&
      inRange(r.gross_price_opportunity_cents / 100, grossRange) &&
      inRange(
        r.pct_below_target !== null && r.pct_below_target !== undefined
          ? r.pct_below_target * 100
          : null,
        belowRange,
      ),
  );

  const chips: ChipDescriptor[] = [];
  if (customer)
    chips.push({
      key: "customer",
      label: entityText(customers.find((c) => c.id === customer)?.name, customer),
      onRemove: () => f.set("customer", null),
    });
  if (item)
    chips.push({
      key: "item",
      label: entityText(itemOpts.find((i) => i.id === item)?.name, item),
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
    <div>
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
            Eligible quantity times the positive gap to the proposed pocket
            price, at unchanged volume - a ceiling, not a forecast.
          </p>
        </div>
        <div className="panel px-5 py-5">
          <h2 className="m-0 text-section">Expected contribution change</h2>
          <p className="m-0 mt-1 text-title font-semibold text-petrol">
            {dollarsShort(o.expected_total_cents)}
          </p>
          <p className="m-0 text-caption secondary">{o.window_label}, causal-supported records only</p>
          <p className="mb-0 mt-2 text-caption secondary">{o.expected_total_note}</p>
        </div>
      </div>
      <Section
        title="Top opportunities"
        lede={`${count(o.n_records)} records in this run; the table shows the largest by gross opportunity, each with the engine's explanation and evidence status.`}
      >
        <FilterBar
          chips={chips}
          onClearAll={() => f.clearAll(FILTER_KEYS)}
          shown={filtered.length}
          total={o.top_opportunities.length}
          noun="opportunities"
        >
          <EntityCombobox label="Customer" options={customers} value={customer} onChange={(v) => f.set("customer", v)} />
          <EntityCombobox label="Item" options={itemOpts} value={item} onChange={(v) => f.set("item", v)} />
          <MultiSelect
            label="Evidence"
            options={evidenceOpts}
            values={evidences}
            onChange={(v) => f.setList("evidence", v)}
            renderOption={(e) => labels(e).label}
          />
          <MultiSelect
            label="Volume treatment"
            options={treatmentOpts}
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
        {filtered.length ? (
          <SortableTable
            data={filtered}
            columns={columns}
            pageSize={10}
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
