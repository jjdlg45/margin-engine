/** Price guidance: a filterable table of customer-item pairs. Filters for
 * customer, item, segment, guidance status, current price (% of list) and gap
 * to floor - all in the URL. Clicking a row opens the band chart and the
 * engine's explanation; direct deep links to a single pair keep working. */

import { useMemo } from "react";
import { BandChart } from "../charts/Band";
import { EntityLabel, entityText } from "../components/EntityLabel";
import { EmptyState, PageHeader, ScrollX } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import { EntityCombobox } from "../components/filters/Combobox";
import { FilterBar } from "../components/filters/FilterBar";
import { MultiSelect } from "../components/filters/MultiSelect";
import { RangeFilter } from "../components/filters/RangeFilter";
import { useBundle } from "../lib/bundle";
import { ChipDescriptor, inRange, rangeChipLabel, useFilterParams } from "../lib/filters";
import { count, dollars, pctOfList } from "../lib/format";
import { useLabels } from "../lib/labels";

const FILTER_KEYS = ["customer", "item", "segment", "status", "pol", "gap"];

export function GuidancePage() {
  const b = useBundle();
  const labels = useLabels();
  const f = useFilterParams();

  const customer = f.params.get("customer");
  const item = f.params.get("item");
  const segments = f.getList("segment");
  const statuses = f.getList("status");
  const polRange = f.getRange("pol"); // current price, % of list
  const gapRange = f.getRange("gap"); // gap to floor, points

  const customers = useMemo(() => {
    const seen = new Map<string, string | null>();
    for (const g of b.guidance) if (!seen.has(g.customer_id)) seen.set(g.customer_id, g.customer_name ?? null);
    return [...seen].map(([id, name]) => ({ id, name }));
  }, [b.guidance]);
  const items = useMemo(() => {
    const seen = new Map<string, string | null>();
    for (const g of b.guidance)
      if ((!customer || g.customer_id === customer) && !seen.has(g.item_id))
        seen.set(g.item_id, g.item_name ?? null);
    return [...seen].map(([id, name]) => ({ id, name }));
  }, [b.guidance, customer]);
  const statusOptions = useMemo(
    () => [...new Set(b.guidance.map((g) => g.status))].sort(),
    [b.guidance],
  );
  const segOptions = useMemo(
    () => [...b.segments.rules].sort((a, z) => a.display_rank - z.display_rank),
    [b.segments.rules],
  );

  const gapToFloor = (g: (typeof b.guidance)[number]): number | null =>
    g.current_index !== null &&
    g.current_index !== undefined &&
    g.floor_index !== null &&
    g.floor_index !== undefined
      ? (g.current_index - g.floor_index) * 100
      : null;

  const filtered = b.guidance.filter(
    (g) =>
      (!customer || g.customer_id === customer) &&
      (!item || g.item_id === item) &&
      (!segments.length || segments.includes(String(g.segment ?? ""))) &&
      (!statuses.length || statuses.includes(g.status)) &&
      inRange(g.current_index !== null && g.current_index !== undefined ? g.current_index * 100 : null, polRange) &&
      inRange(gapToFloor(g), gapRange),
  );

  const pair = customer && item ? filtered.find((g) => g.customer_id === customer && g.item_id === item) ?? b.guidance.find((g) => g.customer_id === customer && g.item_id === item) : undefined;

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
      label: entityText(items.find((i) => i.id === item)?.name, item),
      onRemove: () => f.set("item", null),
    });
  for (const s of statuses)
    chips.push({
      key: `status-${s}`,
      label: labels(s).label,
      onRemove: () => f.setList("status", statuses.filter((x) => x !== s)),
    });
  for (const seg of segments)
    chips.push({
      key: `segment-${seg}`,
      label: `Segment ${segOptions.find((r) => String(r.leaf_id) === seg)?.display_rank ?? seg}`,
      onRemove: () => f.setList("segment", segments.filter((x) => x !== seg)),
    });
  const polChip = rangeChipLabel("Price", polRange, "% of list");
  if (polChip) chips.push({ key: "pol", label: polChip, onRemove: () => f.setRange("pol", { min: null, max: null }) });
  const gapChip = rangeChipLabel("Gap to floor", gapRange, " pts");
  if (gapChip) chips.push({ key: "gap", label: gapChip, onRemove: () => f.setRange("gap", { min: null, max: null }) });

  return (
    <div>
      <PageHeader
        title="Price guidance"
        lede={
          <>
            Historical reference bands within each pair's commercial segment
            and the economic floor that guarantees the configured minimum
            pocket margin. Bands are historical positions, not optimal prices;
            targets stay provisional until elasticity evidence supports
            optimization. {count(b.guidance.length)} pairs in this run.
          </>
        }
      />
      <FilterBar
        chips={chips}
        onClearAll={() => f.clearAll(FILTER_KEYS)}
        shown={filtered.length}
        total={b.guidance.length}
        noun="pairs"
      >
        <EntityCombobox label="Customer" options={customers} value={customer} onChange={(v) => f.set("customer", v)} />
        <EntityCombobox label="Item" options={items} value={item} onChange={(v) => f.set("item", v)} />
        <MultiSelect
          label="Guidance status"
          options={statusOptions}
          values={statuses}
          onChange={(v) => f.setList("status", v)}
          renderOption={(s) => labels(s).label}
        />
        <MultiSelect
          label="Segment"
          options={segOptions.map((r) => String(r.leaf_id))}
          values={segments}
          onChange={(v) => f.setList("segment", v)}
          renderOption={(leaf) => {
            const r = segOptions.find((x) => String(x.leaf_id) === leaf);
            return r ? `Segment ${r.display_rank}` : leaf;
          }}
        />
        <RangeFilter
          label="Current price"
          unit="% of list"
          range={polRange}
          onChange={(r) => f.setRange("pol", r)}
          presets={[{ label: "Below 90% of list", range: { min: null, max: 90 } }]}
        />
        <RangeFilter
          label="Gap to floor"
          unit="pts"
          range={gapRange}
          onChange={(r) => f.setRange("gap", r)}
          presets={[{ label: "Below floor", range: { min: null, max: 0 } }]}
        />
      </FilterBar>

      {pair && (
        <div className="panel mb-6 px-5 py-6 sm:px-8">
          <div className="row items-center">
            <span className="font-medium">
              <EntityLabel name={pair.customer_name} id={pair.customer_id} />
              {" × "}
              <EntityLabel name={pair.item_name} id={pair.item_id} />
            </span>
            <StatusChip status={pair.status} />
            {pair.target_label && <StatusChip status={pair.target_label} />}
            <span className="text-caption secondary">
              {(() => {
                const r = b.segments.rules.find((x) => x.leaf_id === pair.segment);
                return r ? `Segment ${r.display_rank}` : "no segment";
              })()}
              {" · "}
              {dollars(pair.annual_net_revenue_cents)} baseline revenue
            </span>
            <button
              onClick={() => f.set("item", null)}
              className="ml-auto cursor-pointer rounded-badge border hairline bg-transparent px-2 py-1 text-caption secondary hover:text-ink"
            >
              Back to results
            </button>
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
              <dt className="text-caption secondary">Target price (% of list)</dt>
              <dd className="m-0">{pctOfList(pair.target_index)}</dd>
            </div>
          </dl>
          <p className="mb-0 mt-4 max-w-2xl">{pair.explanation}</p>
        </div>
      )}

      {filtered.length ? (
        <ScrollX label="Guidance pairs table">
          <table>
            <thead>
              <tr>
                <th className="left">Customer</th>
                <th className="left">Item</th>
                <th className="left">Segment</th>
                <th>Current price (% of list)</th>
                <th>Gap to floor (pts)</th>
                <th className="left">Status</th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, 50).map((g) => (
                <tr key={`${g.customer_id}-${g.item_id}`}>
                  <td className="left">
                    <EntityLabel name={g.customer_name} id={g.customer_id} />
                  </td>
                  <td className="left">
                    <button
                      className="cursor-pointer border-0 bg-transparent p-0 text-left text-petrol underline-offset-4 hover:underline"
                      onClick={() => {
                        f.set("customer", g.customer_id);
                        f.set("item", g.item_id);
                      }}
                    >
                      <EntityLabel name={g.item_name} id={g.item_id} />
                    </button>
                  </td>
                  <td className="left">
                    {(() => {
                      const r = b.segments.rules.find((x) => x.leaf_id === g.segment);
                      return r ? `Segment ${r.display_rank}` : "—";
                    })()}
                  </td>
                  <td>{pctOfList(g.current_index)}</td>
                  <td>{gapToFloor(g) !== null ? gapToFloor(g)!.toFixed(1) : "—"}</td>
                  <td className="left">
                    <StatusChip status={g.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {filtered.length > 50 && (
            <p className="text-caption secondary">
              Showing the first 50 matches; narrow the filters for the rest.
            </p>
          )}
        </ScrollX>
      ) : (
        <EmptyState title="No pairs match these filters">
          Loosen or clear a filter. Pairs without enough comparable history
          report insufficient data rather than a band position.
        </EmptyState>
      )}
    </div>
  );
}
