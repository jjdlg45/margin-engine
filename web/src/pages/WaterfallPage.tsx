/** Price waterfall detail: overall / by segment / by customer, plus leakage
 * by component in dollars and share. */

import * as Tabs from "@radix-ui/react-tabs";
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { Waterfall } from "../charts/Waterfall";
import { QuerySection } from "../components/Async";
import { EntityCombobox } from "../components/filters/Combobox";
import { EmptyState, PageHeader, Section, ScrollX } from "../components/Page";
import type { WaterfallData } from "../lib/api/types";
import { usePageData } from "../lib/queries";
import { useCompact } from "../lib/useCompact";
import { dollars, dollarsShort, pct } from "../lib/format";

const tabClass =
  "rounded-badge px-3 py-1 text-sm data-[state=active]:bg-petrol data-[state=active]:text-paper text-ink/70 hover:text-ink";

function WaterfallBody({ d }: { d: WaterfallData }) {
  const compact = useCompact();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") ?? "overall";
  const groupKey = params.get("key") ?? "";
  const setGroupKey = (v: string) =>
    setParams(
      (p) => {
        if (v) p.set("key", v);
        else p.delete("key");
        return p;
      },
      { replace: true },
    );
  const segOf = new Map(d.segment_names.map((r) => [String(r.leaf_id), r]));
  const groups = (view === "segment" ? d.by_segment : d.by_customer).map((g) => {
    const seg = view === "segment" ? segOf.get(g.key) : undefined;
    return seg ? { ...g, label: `Segment ${seg.display_rank} — ${seg.name}` } : g;
  });
  const active = useMemo(
    () => groups.find((g) => g.key === groupKey) ?? groups[0],
    [groups, groupKey],
  );
  return (
    <>
      <Tabs.Root
        value={view}
        onValueChange={(v) => {
          setParams((p) => {
            p.set("view", v);
            p.delete("key");
            return p;
          });
        }}
      >
        <Tabs.List aria-label="Waterfall scope" className="row">
          <Tabs.Trigger value="overall" className={tabClass}>
            Overall
          </Tabs.Trigger>
          <Tabs.Trigger value="segment" className={tabClass}>
            By segment
          </Tabs.Trigger>
          <Tabs.Trigger value="customer" className={tabClass}>
            By customer
          </Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="overall">
          <div className="panel mt-4 px-5 pb-5 pt-6 sm:px-8">
            <Waterfall stages={d.overall} compact={compact} />
          </div>
        </Tabs.Content>
        {(["segment", "customer"] as const).map((scope) => (
          <Tabs.Content key={scope} value={scope}>
            <div className="mt-4">
              <EntityCombobox
                label={scope === "segment" ? "Segment" : "Customer"}
                options={groups.map((g) => ({
                  id: g.key,
                  name: g.label,
                  hint: dollarsShort(g.net_revenue_cents),
                }))}
                value={active?.key ?? null}
                onChange={(v) => setGroupKey(v ?? "")}
              />
            </div>
            {active ? (
              <div className="panel mt-3 px-5 pb-5 pt-6 sm:px-8">
                <p className="mb-1 mt-0 text-caption secondary">
                  {active.label}: {dollars(active.net_revenue_cents)} net invoiced revenue on sale lines.
                </p>
                <Waterfall stages={active.stages} compact={compact} />
              </div>
            ) : (
              <EmptyState title={`No ${scope} breakdown available`}>
                The export carries no {scope}-level waterfall for this run.
              </EmptyState>
            )}
          </Tabs.Content>
        ))}
      </Tabs.Root>
      <Section
        title="Leakage by component"
        lede={
          <>
            Where the {dollars(d.leakage_total_cents)} between list and pocket
            went over the {d.window_label}. Shares are of total leakage.
          </>
        }
      >
        <ScrollX label="Leakage table">
          <table className="max-w-xl">
            <thead>
              <tr>
                <th>Component</th>
                <th>Dollars</th>
                <th>Share of leakage</th>
              </tr>
            </thead>
            <tbody>
              {d.leakage_components.map((c) => (
                <tr key={c.component}>
                  <td>{c.component}</td>
                  <td>{dollars(c.amount_cents)}</td>
                  <td>{pct(c.share_of_leakage)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollX>
      </Section>
    </>
  );
}

export function WaterfallPage() {
  const q = usePageData("waterfall");
  return (
    <div>
      <PageHeader
        title="Price waterfall"
        lede={
          <>
            From list value to contribution on sale lines (returns appear in
            Customers &amp; orders), overall or for a single segment or
            customer. Hatched bars are pricing leakage; dotted bars are costs.
          </>
        }
      />
      <QuerySection query={q} label="the price waterfall" lines={8}>
        {(d) => <WaterfallBody d={d} />}
      </QuerySection>
    </div>
  );
}
