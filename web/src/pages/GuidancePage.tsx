/** Price guidance: a filterable table of customer-item pairs. Filters for
 * customer, item, segment, guidance status, current price (% of list) and gap
 * to floor - all in the URL, executed by the data client with the same named
 * vocabulary the planned BFF exposes. Clicking a row opens the band chart and
 * the engine's explanation (fetched lazily); deep links keep working. The
 * results table is virtualized - every filtered row is scrollable, but only
 * the visible ones are in the DOM. */

import { BandChart } from "../charts/Band";
import { QuerySection, Skeleton } from "../components/Async";
import { EntityLabel, entityText } from "../components/EntityLabel";
import { EmptyState, PageHeader } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import { VirtualTable } from "../components/VirtualTable";
import { EntityCombobox } from "../components/filters/Combobox";
import { FilterBar } from "../components/filters/FilterBar";
import { MultiSelect } from "../components/filters/MultiSelect";
import { RangeFilter } from "../components/filters/RangeFilter";
import type { GuidanceData, SegmentName, TableRowMap } from "../lib/api/types";
import { ChipDescriptor, rangeChipLabel, useFilterParams } from "../lib/filters";
import { count, dollars, pctOfList } from "../lib/format";
import { useLabels } from "../lib/labels";
import { useGuidanceExplanation, usePageData, useTableQuery } from "../lib/queries";

const FILTER_KEYS = ["customer", "item", "segment", "status", "pol", "gap"];

type GuidanceRow = TableRowMap["guidance"];

const gapToFloor = (g: GuidanceRow): number | null =>
  g.current_index !== null &&
  g.current_index !== undefined &&
  g.floor_index !== null &&
  g.floor_index !== undefined
    ? (g.current_index - g.floor_index) * 100
    : null;

function segmentName(segments: SegmentName[], leaf: number | null | undefined): string | null {
  const r = segments.find((x) => x.leaf_id === leaf);
  return r ? `Segment ${r.display_rank}` : null;
}

function PairDetail({
  customer,
  item,
  segments,
  onBack,
}: {
  customer: string;
  item: string;
  segments: SegmentName[];
  onBack: () => void;
}) {
  const pairQuery = useTableQuery("guidance", {
    page: 1,
    page_size: 1,
    filters: { customer, item },
  });
  const explanation = useGuidanceExplanation(customer, item);
  return (
    <div className="panel mb-6 px-5 py-6 sm:px-8">
      <QuerySection query={pairQuery} label="pair detail" lines={6}>
        {(page) => {
          const pair = page.items[0];
          if (!pair) {
            return (
              <EmptyState title="Pair not found">
                No guidance pair exists for this customer and item in this run.
              </EmptyState>
            );
          }
          return (
            <>
              <div className="row items-center">
                <span className="font-medium">
                  <EntityLabel name={pair.customer_name} id={pair.customer_id} />
                  {" × "}
                  <EntityLabel name={pair.item_name} id={pair.item_id} />
                </span>
                <StatusChip status={pair.status} />
                {pair.target_label && <StatusChip status={pair.target_label} />}
                <span className="text-caption secondary">
                  {segmentName(segments, pair.segment) ?? "no segment"}
                  {" · "}
                  {dollars(pair.annual_net_revenue_cents)} baseline revenue
                </span>
                <button
                  onClick={onBack}
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
              {explanation.isPending ? (
                <Skeleton label="explanation" lines={2} />
              ) : explanation.isError ? (
                <p className="mb-0 mt-4 max-w-2xl text-caption secondary">
                  The explanation for this pair could not be loaded.
                </p>
              ) : (
                <p className="mb-0 mt-4 max-w-2xl">{explanation.data}</p>
              )}
            </>
          );
        }}
      </QuerySection>
    </div>
  );
}

function GuidanceBody({ facets }: { facets: GuidanceData["facets"] }) {
  const labels = useLabels();
  const f = useFilterParams();

  const customer = f.params.get("customer");
  const item = f.params.get("item");
  const segments = f.getList("segment");
  const statuses = f.getList("status");
  const polRange = f.getRange("pol"); // current price, % of list
  const gapRange = f.getRange("gap"); // gap to floor, points

  const table = useTableQuery("guidance", {
    page: 1,
    page_size: 5000,
    filters: {
      customer: customer ?? undefined,
      item: item ?? undefined,
      segments: segments.length ? segments : undefined,
      statuses: statuses.length ? statuses : undefined,
      pol_min: polRange.min ?? undefined,
      pol_max: polRange.max ?? undefined,
      gap_min: gapRange.min ?? undefined,
      gap_max: gapRange.max ?? undefined,
    },
  });

  const segOptions = [...facets.segments].sort((a, z) => a.display_rank - z.display_rank);

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
    <>
      {/* The filter controls live outside the results QuerySection: a result
          refetch must never unmount the bar (it would drop combobox state
          mid-interaction); only the results below swap to a skeleton. */}
      <FilterBar
        chips={chips}
        onClearAll={() => f.clearAll(FILTER_KEYS)}
        shown={table.data?.total}
        total={table.data?.total_unfiltered}
        noun="pairs"
      >
        <EntityCombobox label="Customer" options={facets.customers} value={customer} onChange={(v) => f.set("customer", v)} />
        <EntityCombobox label="Item" options={facets.items} value={item} onChange={(v) => f.set("item", v)} />
        <MultiSelect
          label="Guidance status"
          options={facets.statuses}
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

      {customer && item && (
        <PairDetail
          customer={customer}
          item={item}
          segments={facets.segments}
          onBack={() => f.set("item", null)}
        />
      )}

      <QuerySection query={table} label="guidance results" lines={8}>
        {(page) =>
          page.items.length ? (
            <VirtualTable
              rows={page.items}
              label="Guidance pairs table"
              head={
                <tr>
                  <th className="left">Customer</th>
                  <th className="left">Item</th>
                  <th className="left">Segment</th>
                  <th>Current price (% of list)</th>
                  <th>Gap to floor (pts)</th>
                  <th className="left">Status</th>
                </tr>
              }
              renderRow={(g) => (
                <>
                  <td className="left whitespace-nowrap">
                    <EntityLabel name={g.customer_name} id={g.customer_id} />
                  </td>
                  <td className="left whitespace-nowrap">
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
                  <td className="left">{segmentName(facets.segments, g.segment) ?? "—"}</td>
                  <td>{pctOfList(g.current_index)}</td>
                  <td>{gapToFloor(g) !== null ? gapToFloor(g)!.toFixed(1) : "—"}</td>
                  <td className="left">
                    <StatusChip status={g.status} />
                  </td>
                </>
              )}
            />
          ) : (
            <EmptyState title="No pairs match these filters">
              Loosen or clear a filter. Pairs without enough comparable history
              report insufficient data rather than a band position.
            </EmptyState>
          )
        }
      </QuerySection>
    </>
  );
}

export function GuidancePage() {
  const page = usePageData("guidance");
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
            optimization.
            {page.data ? <> {count(page.data.n_pairs)} pairs in this run.</> : null}
          </>
        }
      />
      <QuerySection query={page} label="guidance filters" lines={4}>
        {(d) => <GuidanceBody facets={d.facets} />}
      </QuerySection>
    </div>
  );
}
