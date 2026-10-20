/** Segments: readable context-only rules with sizes, the per-segment price
 * index distribution, and the separately-labeled margin-driver tree. */

import { PageHeader, Section, ScrollX } from "../components/Page";
import { useBundle } from "../lib/bundle";
import { count, num, pct, pctOfList, signedPct } from "../lib/format";

interface Rule {
  leaf_id: number;
  display_rank: number;
  name: string;
  plain: string;
  technical_note: string;
  rule: string;
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
                <details className="text-caption secondary">
                  <summary>Technical rule</summary>
                  <span>
                    Leaf {r.leaf_id}. {r.technical_note}
                  </span>
                  <code className="block font-mono">{r.rule}</code>
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

export function SegmentsPage() {
  const b = useBundle();
  const s = b.segments;
  const rankOf = new Map(s.rules.map((r) => [r.leaf_id, r.display_rank]));
  return (
    <div>
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
        lede="P25 / P50 / P75 of the realized price index across the segment's customer-item pairs."
      >
        <ScrollX label="Segment table">
          <table className="max-w-2xl">
            <thead>
              <tr>
                <th className="left">Segment</th>
                <th>Pairs</th>
                <th>P25 (% of list)</th>
                <th>P50 (% of list)</th>
                <th>P75 (% of list)</th>
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
                    <td>{(100 * (d.p75 - d.p25)).toFixed(1)} pts</td>
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
    </div>
  );
}
