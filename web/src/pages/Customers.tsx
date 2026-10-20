/** Customers & orders: sortable, filterable customer economics table, and a
 * per-customer drill-down (waterfall, top items, guidance statuses, service
 * flags). Returns impact is shown separately and in the net, matching the
 * engine's covered-basis identity. */

import { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Waterfall } from "../charts/Waterfall";
import { EmptyState, PageHeader, Section, ScrollX } from "../components/Page";
import { SortableTable } from "../components/SortableTable";
import { StatusChip } from "../components/StatusChip";
import { useBundle } from "../lib/bundle";
import { useLabels } from "../lib/labels";
import { useCompact } from "../lib/useCompact";
import { count, dollars, pct } from "../lib/format";
import type { CustomerEconomics } from "../types/bundle";

export function Customers() {
  const b = useBundle();
  const labels = useLabels();
  const compact = useCompact();
  const [params, setParams] = useSearchParams();
  const selected = params.get("customer");
  const customer = b.economics.customers.find((c) => c.customer_id === selected);

  const columns = useMemo<ColumnDef<CustomerEconomics, unknown>[]>(
    () => [
      {
        header: "Customer",
        accessorKey: "customer_id",
        cell: (c) => (
          <button
            className="cursor-pointer border-0 bg-transparent p-0 font-mono text-petrol underline-offset-4 hover:underline"
            onClick={() =>
              setParams((p) => {
                p.set("customer", c.row.original.customer_id);
                return p;
              })
            }
          >
            {c.row.original.customer_id}
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
    <div>
      <PageHeader
        title="Customers & orders"
        lede={
          <>
            Contribution on the covered (cost-known) basis, with linked returns
            included and shown separately. The after-activity measure subtracts
            config-rate cost-to-serve and is labeled estimated (the small
            marker). {count(b.economics.orders.n_orders)} orders;{" "}
            {count(b.economics.orders.negative_contribution_orders)} lose money
            after cost-to-serve; order minimum{" "}
            {dollars(b.economics.orders.min_order_value_cents)}.
          </>
        }
      />
      {customer ? (
        <CustomerDetail customer={customer} compact={compact} onBack={() => setParams((p) => {
          p.delete("customer");
          return p;
        })} />
      ) : (
        <SortableTable
          data={b.economics.customers}
          columns={columns}
          filterPlaceholder="Filter customers"
          filterUrlParam="filter"
          initialSort={[{ id: "contribution_after_activity_cents", desc: false }]}
        />
      )}
    </div>
  );
}

function CustomerDetail({
  customer,
  compact,
  onBack,
}: {
  customer: CustomerEconomics;
  compact: boolean;
  onBack: () => void;
}) {
  const b = useBundle();
  const labels = useLabels();
  const wf = b.waterfall_by_customer.find((g) => g.key === customer.customer_id);
  const pairs = b.guidance.filter((g) => g.customer_id === customer.customer_id);
  const topItems = [...pairs]
    .sort((a, z) => (z.annual_net_revenue_cents ?? 0) - (a.annual_net_revenue_cents ?? 0))
    .slice(0, 8);
  return (
    <div>
      <button
        onClick={onBack}
        className="cursor-pointer border-0 bg-transparent p-0 font-medium text-petrol underline-offset-4 hover:underline"
      >
        Back to all customers
      </button>
      <h2 className="heading-rule mb-2 mt-4 text-section">
        <span className="font-mono">{customer.customer_id}</span>
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
      {wf ? (
        <div className="panel mt-4 px-5 pb-5 pt-6 sm:px-8">
          <Waterfall stages={wf.stages} compact={compact} />
        </div>
      ) : (
        <EmptyState title="No waterfall for this customer">
          Per-customer waterfalls cover the top customers by revenue; this one
          is outside that set, so its stages are not in the bundle.
        </EmptyState>
      )}
      <Section
        title="Top items by baseline revenue"
        lede="Guidance status per customer-item pair; the Price guidance page has the band chart and full explanation."
      >
        {topItems.length ? (
          <ScrollX label="Customer items table">
            <table className="max-w-3xl">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>Baseline revenue</th>
                  <th>Current index</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {topItems.map((g) => (
                  <tr key={g.item_id}>
                    <td className="font-mono">{g.item_id}</td>
                    <td>{dollars(g.annual_net_revenue_cents)}</td>
                    <td>{g.current_index?.toFixed(3) ?? "—"}</td>
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
        )}
      </Section>
    </div>
  );
}
