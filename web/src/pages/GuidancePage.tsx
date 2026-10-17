/** Price guidance: customer x item lookup with the band chart (floor,
 * reference band, current price), status badge, and the engine's
 * plain-English explanation. Targets stay provisional unless optimization is
 * supported. */

import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { BandChart } from "../charts/Band";
import { EmptyState, PageHeader } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import { useBundle } from "../lib/bundle";
import { count, dollars } from "../lib/format";

export function GuidancePage() {
  const b = useBundle();
  const [params, setParams] = useSearchParams();
  const customer = params.get("customer") ?? "";
  const item = params.get("item") ?? "";
  const set = (k: string, v: string) =>
    setParams((p) => {
      if (v) p.set(k, v);
      else p.delete(k);
      return p;
    });

  const customers = useMemo(
    () => [...new Set(b.guidance.map((g) => g.customer_id))].sort(),
    [b.guidance],
  );
  const items = useMemo(
    () =>
      [...new Set(b.guidance.filter((g) => !customer || g.customer_id === customer).map((g) => g.item_id))].sort(),
    [b.guidance, customer],
  );
  const matches = b.guidance.filter(
    (g) => (!customer || g.customer_id === customer) && (!item || g.item_id === item),
  );
  const pair = customer && item ? matches[0] : undefined;

  return (
    <div>
      <PageHeader
        title="Price guidance"
        lede={
          <>
            Historical reference bands within the pair's commercial segment and
            the economic floor that guarantees the configured minimum pocket
            margin. Bands are historical positions, not optimal prices; targets
            are labeled provisional until elasticity evidence supports
            optimization. {count(b.guidance.length)} pairs in this run.
          </>
        }
      />
      <div className="row" role="search" aria-label="Pair lookup">
        <label className="text-caption secondary" htmlFor="g-cust">
          Customer
        </label>
        <select id="g-cust" value={customer} onChange={(e) => set("customer", e.target.value)}>
          <option value="">any</option>
          {customers.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
        <label className="text-caption secondary" htmlFor="g-item">
          Item
        </label>
        <select id="g-item" value={item} onChange={(e) => set("item", e.target.value)}>
          <option value="">any</option>
          {items.map((i) => (
            <option key={i}>{i}</option>
          ))}
        </select>
      </div>
      {pair ? (
        <div className="panel mt-5 px-5 py-6 sm:px-8">
          <div className="row items-center">
            <span className="font-mono">
              {pair.customer_id} {"×"} {pair.item_id}
            </span>
            <StatusChip status={pair.status} />
            {pair.target_label && <StatusChip status={pair.target_label} />}
            <span className="text-caption secondary">
              segment {pair.segment ?? "—"}{" · "}
              {dollars(pair.annual_net_revenue_cents)} baseline revenue
            </span>
          </div>
          <div className="mt-4">
            <BandChart
              p25={pair.band_p25}
              p50={pair.band_p50}
              p75={pair.band_p75}
              floor={pair.floor_index}
              current={pair.current_index}
              target={pair.target_index}
            />
          </div>
          <dl className="m-0 mt-2 grid max-w-xl grid-cols-2 gap-x-8 gap-y-2 sm:grid-cols-3">
            <div>
              <dt className="text-caption secondary">Pocket floor</dt>
              <dd className="m-0">{dollars(pair.pocket_floor_cents)}</dd>
            </div>
            <div>
              <dt className="text-caption secondary">Invoice floor</dt>
              <dd className="m-0">{dollars(pair.invoice_floor_cents)}</dd>
            </div>
            <div>
              <dt className="text-caption secondary">Target index</dt>
              <dd className="m-0">{pair.target_index?.toFixed(3) ?? "—"}</dd>
            </div>
          </dl>
          <p className="mb-0 mt-4 max-w-2xl">{pair.explanation}</p>
        </div>
      ) : customer || item ? (
        <div className="mt-5">
          {matches.length ? (
            <div className="overflow-x-auto">
              <table className="max-w-2xl">
                <thead>
                  <tr>
                    <th>Customer</th>
                    <th>Item</th>
                    <th>Current index</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {matches.slice(0, 40).map((g) => (
                    <tr key={`${g.customer_id}-${g.item_id}`}>
                      <td className="font-mono">{g.customer_id}</td>
                      <td className="font-mono">
                        <button
                          className="cursor-pointer border-0 bg-transparent p-0 font-mono text-petrol underline-offset-4 hover:underline"
                          onClick={() => {
                            set("customer", g.customer_id);
                            set("item", g.item_id);
                          }}
                        >
                          {g.item_id}
                        </button>
                      </td>
                      <td>{g.current_index?.toFixed(3) ?? "—"}</td>
                      <td>
                        <StatusChip status={g.status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {matches.length > 40 && (
                <p className="text-caption secondary">
                  Showing 40 of {count(matches.length)} matches; narrow the lookup.
                </p>
              )}
            </div>
          ) : (
            <EmptyState title="No guidance for that selection">
              The pair has no sale history in this run, or too few comparable
              observations exist to place it against a band - in that case the
              engine reports insufficient data instead of a position.
            </EmptyState>
          )}
        </div>
      ) : (
        <p className="mt-5 text-caption secondary">
          Pick a customer and item to see the band chart, floors, status and
          the engine's explanation.
        </p>
      )}
    </div>
  );
}
