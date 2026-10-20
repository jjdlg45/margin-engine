/** Price waterfall detail: overall / by segment / by customer, plus leakage
 * by component in dollars and share. */

import * as Tabs from "@radix-ui/react-tabs";
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { Waterfall } from "../charts/Waterfall";
import { EmptyState, PageHeader, Section, ScrollX } from "../components/Page";
import { useBundle } from "../lib/bundle";
import { useCompact } from "../lib/useCompact";
import { dollars, dollarsShort, pct } from "../lib/format";

const tabClass =
  "rounded-badge px-3 py-1 text-sm data-[state=active]:bg-petrol data-[state=active]:text-paper text-ink/70 hover:text-ink";

export function WaterfallPage() {
  const b = useBundle();
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
  const segName = new Map(b.segments.rules.map((r) => [String(r.leaf_id), r.name]));
  const groups = (view === "segment" ? b.waterfall_by_segment : b.waterfall_by_customer).map(
    (g) =>
      view === "segment" && segName.has(g.key)
        ? { ...g, label: `${g.label} — ${segName.get(g.key)}` }
        : g,
  );
  const active = useMemo(
    () => groups.find((g) => g.key === groupKey) ?? groups[0],
    [groups, groupKey],
  );
  const leakTotal = b.opportunity.leakage_total_cents;
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
            <Waterfall stages={b.waterfall_overall} compact={compact} />
          </div>
        </Tabs.Content>
        {(["segment", "customer"] as const).map((scope) => (
          <Tabs.Content key={scope} value={scope}>
            <div className="row mt-4">
              <label className="text-caption secondary" htmlFor={`${scope}-pick`}>
                {scope === "segment" ? "Segment" : "Customer"}
              </label>
              <select
                id={`${scope}-pick`}
                value={active?.key ?? ""}
                onChange={(e) => setGroupKey(e.target.value)}
              >
                {groups.map((g) => (
                  <option key={g.key} value={g.key}>
                    {g.label} ({dollarsShort(g.net_revenue_cents)})
                  </option>
                ))}
              </select>
            </div>
            {active ? (
              <div className="panel mt-3 px-5 pb-5 pt-6 sm:px-8">
                <p className="mb-1 mt-0 text-caption secondary">
                  {active.label}: {dollars(active.net_revenue_cents)} net revenue on sale lines.
                </p>
                <Waterfall stages={active.stages} compact={compact} />
              </div>
            ) : (
              <EmptyState title={`No ${scope} breakdown available`}>
                The bundle carries no {scope}-level waterfall for this run.
              </EmptyState>
            )}
          </Tabs.Content>
        ))}
      </Tabs.Root>
      <Section
        title="Leakage by component"
        lede={
          <>
            Where the {dollars(leakTotal)} between list and pocket went over
            the {b.opportunity.window_label}. Shares are of total leakage.
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
              {b.leakage_components_overall.map((c) => (
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
    </div>
  );
}
