/** Segments: readable context-only rules with sizes, the per-segment price
 * index distribution, and the separately-labeled margin-driver tree. */

import { QuerySection } from "../components/Async";
import { PageHeader, Section, ScrollX } from "../components/Page";
import type { SegmentsSection } from "../types/bundle";
import { usePageData } from "../lib/queries";
import { count, num, pct, pctOfList, signedPct } from "../lib/format";

interface Rule {
  leaf_id: number;
  display_rank: number;
  name: string;
  plain: string;
  conditions: string[];
  technical_note: string;
  meaning?: string | null;
  n_lines: number;
  n_customers: number;
  n_items: number;
  mean_target: number;
}

function RuleTable({
  rules,
  target,
  rowWord,
  fmt,
}: {
  rules: Rule[];
  target: string;
  rowWord: string;
  fmt: (v: number) => string;
}) {
  const ranked = [...rules].sort((a, z) => a.display_rank - z.display_rank);
  return (
    <ScrollX label="Segment table">
      <table>
        <thead>
          <tr>
            <th className="left">{rowWord}</th>
            <th className="left">Rule</th>
            <th>Lines</th>
            <th>Customers</th>
            <th>Items</th>
            <th>{target}</th>
          </tr>
        </thead>
        <tbody>
          {ranked.map((r) => (
            <tr key={r.leaf_id}>
              <td className="left whitespace-nowrap">{rowWord} {r.display_rank}</td>
              <td className="left max-w-xl">
                <span className="font-medium">{r.name}</span>
                <span className="block text-caption secondary">{r.plain}</span>
                {r.meaning && <span className="block text-caption">{r.meaning}</span>}
                <details className="text-caption secondary">
                  <summary>How this group is defined</summary>
                  <ul className="my-1 list-disc pl-5">
                    {r.conditions.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                  {r.technical_note && <span>{r.technical_note}</span>}
                </details>
              </td>
              <td>{count(r.n_lines)}</td>
              <td>{count(r.n_customers)}</td>
              <td>{count(r.n_items)}</td>
              <td>{fmt(r.mean_target)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollX>
  );
}

function SegmentsBody({ s }: { s: SegmentsSection }) {
  const rankOf = new Map(s.rules.map((r) => [r.leaf_id, r.display_rank]));
  return (
    <>
      <PageHeader
        title="Segments"
        lede={
          <>
            Commercial segments come from a decision tree on the normalized
            price index using context features only - no realized price,
            discount, cost or rebate columns, so the segments cannot leak the
            answer. Bootstrap stability (median ARI):{" "}
            {s.bootstrap_median_ari !== null ? num(s.bootstrap_median_ari, 3) : "—"};
            price recommendations changing across resamples:{" "}
            {s.recommendation_change_share !== null ? pct(s.recommendation_change_share) : "—"}.
          </>
        }
      />
      <Section title="Segment rules" lede="Each leaf is a readable conjunction of context conditions.">
        <RuleTable rules={s.rules} target="Median price (% of list)" rowWord="Segment" fmt={pctOfList} />
      </Section>
      <Section
        title="Price-index distribution per segment"
        lede="Lower quartile, median and upper quartile of the realized price index across the segment's customer-item pairs."
      >
        <ScrollX label="Segment table">
          <table className="max-w-2xl">
            <thead>
              <tr>
                <th className="left">Segment</th>
                <th>Pairs</th>
                <th>Lower quartile (% of list)</th>
                <th>Median (% of list)</th>
                <th>Upper quartile (% of list)</th>
                <th>Spread</th>
              </tr>
            </thead>
            <tbody>
              {[...s.index_distribution]
                .sort((a, z) => (rankOf.get(a.segment) ?? 99) - (rankOf.get(z.segment) ?? 99))
                .map((d) => (
                  <tr key={d.segment}>
                    <td className="left">Segment {rankOf.get(d.segment) ?? d.segment}</td>
                    <td>{count(d.n_pairs)}</td>
                    <td>{pctOfList(d.p25)}</td>
                    <td>{pctOfList(d.p50)}</td>
                    <td>{pctOfList(d.p75)}</td>
                    <td>{(100 * (d.p75 - d.p25)).toFixed(1)} points</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </ScrollX>
      </Section>
      <Section
        title="Margin drivers (explanatory, not segments)"
        lede={s.margin_driver_note}
      >
        <RuleTable rules={s.margin_driver_rules} target="Mean pocket margin" rowWord="Driver" fmt={signedPct} />
      </Section>
    </>
  );
}

export function SegmentsPage() {
  const q = usePageData("segments");
  return (
    <div>
      <QuerySection query={q} label="segments" lines={8}>
        {(s) => <SegmentsBody s={s} />}
      </QuerySection>
    </div>
  );
}
