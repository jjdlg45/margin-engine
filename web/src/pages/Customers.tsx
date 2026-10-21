/** Customers & orders: sortable, filterable customer economics table, and a
 * per-customer drill-down (waterfall, top items, guidance statuses, service
 * flags). Returns impact is shown separately and in the net, matching the
 * engine's covered-basis identity. All rows come through the data client. */

import { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Waterfall } from "../charts/Waterfall";
import { QuerySection } from "../components/Async";
import { EntityLabel, entityText } from "../components/EntityLabel";
import { EntityCombobox } from "../components/filters/Combobox";
import { FilterBar } from "../components/filters/FilterBar";
import { MultiSelect } from "../components/filters/MultiSelect";
import { RangeFilter } from "../components/filters/RangeFilter";
import { EmptyState, PageHeader, Section, ScrollX } from "../components/Page";
import { SortableTable } from "../components/SortableTable";
import { StatusChip } from "../components/StatusChip";
import type { CustomersData } from "../lib/api/types";
import { ChipDescriptor, rangeChipLabel, useFilterParams } from "../lib/filters";
import { useLabels } from "../lib/labels";
import { usePageData, useTableQuery } from "../lib/queries";
import { useCompact } from "../lib/useCompact";
import { count, dollars, pct, pctOfList } from "../lib/format";
import type { CustomerEconomics } from "../types/bundle";

const FILTER_KEYS = ["cust", "flags", "losing", "returns", "excluded", "orders"];

function CustomersTable({ d }: { d: CustomersData }) {
  const labels = useLabels();
  const f = useFilterParams();
  const [, setParams] = useSearchParams();

  const custFilter = f.params.get("cust");
  const flagFilter = f.getList("flags");
  const losing = f.params.get("losing") === "1";
  const returnsRange = f.getRange("returns");
  const excludedRange = f.getRange("excluded");
  const ordersRange = f.getRange("orders");

  const table = useTableQuery("customers", {
    page: 1,
    page_size: 5000,
    filters: {
      customer: custFilter ?? undefined,
      flags: flagFilter.length ? flagFilter : undefined,
      losing: losing || undefined,
      returns_min: returnsRange.min ?? undefined,
      returns_max: returnsRange.max ?? undefined,
      excluded_min: excludedRange.min ?? undefined,
      excluded_max: excludedRange.max ?? undefined,
      orders_min: ordersRange.min ?? undefined,
      orders_max: ordersRange.max ?? undefined,
    },
  });

  const chips: ChipDescriptor[] = [];
  if (custFilter)
    chips.push({
      key: "cust",
      label: entityText(d.facets.customers.find((c) => c.id === custFilter)?.name, custFilter),
      onRemove: () => f.set("cust", null),
    });
  for (const fl of flagFilter)
    chips.push({
      key: `fl-${fl}`,
      label: labels(fl).label,
      onRemove: () => f.setList("flags", flagFilter.filter((x) => x !== fl)),
    });
  if (losing)
    chips.push({
      key: "losing",
      label: "Losing money after cost-to-serve",
      onRemove: () => f.set("losing", null),
    });
  const rChip = rangeChipLabel("Returns impact", returnsRange, " $");
  if (rChip)
    chips.push({ key: "returns", label: rChip, onRemove: () => f.setRange("returns", { min: null, max: null }) });
  const eChip = rangeChipLabel("Revenue excluded", excludedRange, "%");
  if (eChip)
    chips.push({ key: "excluded", label: eChip, onRemove: () => f.setRange("excluded", { min: null, max: null }) });
  const oChip = rangeChipLabel("Orders", ordersRange);
  if (oChip)
    chips.push({ key: "orders", label: oChip, onRemove: () => f.setRange("orders", { min: null, max: null }) });

  const columns = useMemo<ColumnDef<CustomerEconomics, unknown>[]>(
    () => [
      {
        header: "Customer",
        accessorKey: "customer_id",
        meta: { left: true },
        cell: (c) => (
          <button
            className="cursor-pointer border-0 bg-transparent p-0 text-left text-petrol underline-offset-4 hover:underline"
            onClick={() =>
              setParams((p) => {
                p.set("customer", c.row.original.customer_id);
                return p;
              })
            }
          >
            <EntityLabel name={c.row.original.customer_name} id={c.row.original.customer_id} />
          </button>
        ),
      },
      { header: "Orders", accessorKey: "n_orders", cell: (c) => count(c.row.original.n_orders) },
      {
        header: "Net revenue",
        accessorKey: "net_revenue_cents",
        cell: (c) => dollars(c.row.original.net_revenue_cents),
      },
      {
        header: "Returns impact",
        accessorKey: "returns_contribution_cents",
        cell: (c) => dollars(c.row.original.returns_contribution_cents),
      },
      {
        header: "Contribution",
        accessorKey: "contribution_cents",
        cell: (c) => dollars(c.row.original.contribution_cents),
      },
      {
        header: "After activity cost",
        accessorKey: "contribution_after_activity_cents",
        cell: (c) => (
          <span>
            {dollars(c.row.original.contribution_after_activity_cents)}
            <span className="est-marker" aria-hidden="true" />
          </span>
        ),
      },
      {
        header: "% revenue excluded",
        accessorKey: "pct_revenue_excluded",
        cell: (c) => pct(c.row.original.pct_revenue_excluded),
      },
      {
        header: "Service flags",
        accessorKey: "service_flags",
        enableSorting: false,
        cell: (c) =>
          c.row.original.service_flags.length ? (
            <span className="chip chip-hatched" title={c.row.original.service_flags.join(", ")}>
              {c.row.original.service_flags.map((f) => labels(f).label).join(", ")}
            </span>
          ) : (
            "—"
          ),
      },
    ],
    [setParams, labels],
  );

  return (
    <QuerySection query={table} label="customer economics" lines={8}>
      {(page) => (
        <>
          <FilterBar
            chips={chips}
            onClearAll={() => f.clearAll(FILTER_KEYS)}
            shown={page.total}
            total={page.total_unfiltered}
            noun="customers"
          >
            <EntityCombobox
              label="Customer"
              options={d.facets.customers}
              value={custFilter}
              onChange={(v) => f.set("cust", v)}
            />
            <MultiSelect
              label="Service flags"
              options={d.facets.service_flags}
              values={flagFilter}
              onChange={(v) => f.setList("flags", v)}
              renderOption={(fl) => labels(fl).label}
            />
            <div className="flex flex-col gap-1">
              <span className="text-caption secondary">After cost-to-serve</span>
              <button
                type="button"
                aria-pressed={losing}
                onClick={() => f.set("losing", losing ? null : "1")}
                className={
                  losing
                    ? "rounded-badge border border-petrol bg-petrol px-2 py-1 text-sm text-paper"
                    : "rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/70"
                }
              >
                Losing money only
              </button>
            </div>
            <RangeFilter label="Returns impact" unit="$" range={returnsRange} onChange={(r) => f.setRange("returns", r)} />
            <RangeFilter label="Revenue excluded" unit="%" range={excludedRange} onChange={(r) => f.setRange("excluded", r)} />
            <RangeFilter label="Orders" range={ordersRange} onChange={(r) => f.setRange("orders", r)} />
          </FilterBar>
          <SortableTable
            data={page.items}
            columns={columns}
            initialSort={[{ id: "contribution_after_activity_cents", desc: false }]}
          />
        </>
      )}
    </QuerySection>
  );
}

export function Customers() {
  const page = usePageData("customers");
  const compact = useCompact();
  const [params, setParams] = useSearchParams();
  const selected = params.get("customer");
  return (
    <div>
      <QuerySection query={page} label="customer economics" lines={8}>
        {(d) => (
          <>
            <PageHeader
              title="Customers & orders"
              lede={
                <>
                  Contribution on the covered (cost-known) basis, with linked returns
                  included and shown separately. The after-activity measure subtracts
                  config-rate cost-to-serve and is labeled estimated (the small
                  marker). {count(d.orders.n_orders)} orders;{" "}
                  {count(d.orders.negative_contribution_orders)} lose money
                  after cost-to-serve; order minimum{" "}
                  {dollars(d.orders.min_order_value_cents)}.
                </>
              }
            />
            {selected ? (
              <CustomerDetail
                customerId={selected}
                d={d}
                compact={compact}
                onBack={() =>
                  setParams((p) => {
                    p.delete("customer");
                    return p;
                  })
                }
              />
            ) : (
              <CustomersTable d={d} />
            )}
          </>
        )}
      </QuerySection>
    </div>
  );
}

function CustomerDetail({
  customerId,
  d,
  compact,
  onBack,
}: {
  customerId: string;
  d: CustomersData;
  compact: boolean;
  onBack: () => void;
}) {
  const labels = useLabels();
  const rowQuery = useTableQuery("customers", {
    page: 1,
    page_size: 1,
    filters: { customer: customerId },
  });
  const pairsQuery = useTableQuery("guidance", {
    page: 1,
    page_size: 5000,
    filters: { customer: customerId },
  });
  const wf = d.waterfall_by_customer.find((g) => g.key === customerId);
  return (
    <div>
      <button
        onClick={onBack}
        className="cursor-pointer border-0 bg-transparent p-0 font-medium text-petrol underline-offset-4 hover:underline"
      >
        Back to all customers
      </button>
      <QuerySection query={rowQuery} label="customer detail" lines={4}>
        {(page) => {
          const customer = page.items[0];
          if (!customer) {
            return (
              <EmptyState title="Customer not found">
                No economics exist for this customer in this run.
              </EmptyState>
            );
          }
          return (
            <>
              <h2 className="heading-rule mb-2 mt-4 text-section">
                <EntityLabel name={customer.customer_name} id={customer.customer_id} />
              </h2>
              <div className="row text-caption secondary">
                <span>{count(customer.n_orders)} orders</span>
                <span>{dollars(customer.net_revenue_cents)} net revenue</span>
                <span>{dollars(customer.returns_contribution_cents)} returns impact</span>
                <span>{pct(customer.pct_revenue_excluded)} revenue excluded (cost unknown)</span>
                {customer.service_flags.map((f) => (
                  <span key={f} className="chip chip-hatched" title={f}>
                    {labels(f).label}
                  </span>
                ))}
              </div>
            </>
          );
        }}
      </QuerySection>
      {wf ? (
        <div className="panel mt-4 px-5 pb-5 pt-6 sm:px-8">
          <Waterfall stages={wf.stages} compact={compact} />
        </div>
      ) : (
        <EmptyState title="No waterfall for this customer">
          Per-customer waterfalls cover the top customers by revenue; this one
          is outside that set, so its stages are not in the export.
        </EmptyState>
      )}
      <Section
        title="Top items by baseline revenue"
        lede="Guidance status per customer-item pair; the Price guidance page has the band chart and full explanation."
      >
        <QuerySection query={pairsQuery} label="top items" lines={5}>
          {(page) => {
            const topItems = [...page.items]
              .sort((a, z) => (z.annual_net_revenue_cents ?? 0) - (a.annual_net_revenue_cents ?? 0))
              .slice(0, 8);
            return topItems.length ? (
              <ScrollX label="Customer items table">
                <table className="max-w-3xl">
                  <thead>
                    <tr>
                      <th>Item</th>
                      <th>Baseline revenue</th>
                      <th>Current price (% of list)</th>
                      <th>Status</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {topItems.map((g) => (
                      <tr key={g.item_id}>
                        <td className="left">
                          <EntityLabel name={g.item_name} id={g.item_id} />
                        </td>
                        <td>{dollars(g.annual_net_revenue_cents)}</td>
                        <td>{pctOfList(g.current_index)}</td>
                        <td>
                          <StatusChip status={g.status} />
                        </td>
                        <td>
                          <Link
                            className="text-petrol underline-offset-4 hover:underline"
                            to={`/guidance?customer=${g.customer_id}&item=${g.item_id}`}
                          >
                            Guidance
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollX>
            ) : (
              <EmptyState title="No guidance pairs">
                This customer has no pairs with enough history for guidance.
              </EmptyState>
            );
          }}
        </QuerySection>
      </Section>
    </div>
  );
}
