/** Segments: readable context-only rules with sizes, the per-segment price
 * index distribution, and the separately-labeled margin-driver tree. */

import { PageHeader, Section } from "../components/Page";
import { useBundle } from "../lib/bundle";
import { count, num, pct } from "../lib/format";

interface Rule {
  leaf_id: number;
  rule: string;
  n_lines: number;
  n_customers: number;
  n_items: number;
  mean_target: number;
}

function RuleTable({ rules, target }: { rules: Rule[]; target: string }) {
  return (
    <div className="overflow-x-auto">
      <table>
        <thead>
          <tr>
            <th>Leaf</th>
            <th>Rule</th>
            <th>Lines</th>
            <th>Customers</th>
            <th>Items</th>
            <th>{target}</th>
          </tr>
        </thead>
        <tbody>
          {rules.map((r) => (
            <tr key={r.leaf_id}>
              <td>{r.leaf_id}</td>
              <td className="left max-w-xl font-mono text-caption">{r.rule}</td>
              <td>{count(r.n_lines)}</td>
              <td>{count(r.n_customers)}</td>
              <td>{count(r.n_items)}</td>
              <td>{num(r.mean_target, 3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SegmentsPage() {
  const b = useBundle();
  const s = b.segments;
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
        <RuleTable rules={s.rules} target="Median price index" />
      </Section>
      <Section
        title="Price-index distribution per segment"
        lede="P25 / P50 / P75 of the realized price index across the segment's customer-item pairs."
      >
        <div className="overflow-x-auto">
          <table className="max-w-2xl">
            <thead>
              <tr>
                <th>Segment</th>
                <th>Pairs</th>
                <th>P25</th>
                <th>P50</th>
                <th>P75</th>
                <th>Spread (P75 - P25)</th>
              </tr>
            </thead>
            <tbody>
              {s.index_distribution.map((d) => (
                <tr key={d.segment}>
                  <td>{d.segment}</td>
                  <td>{count(d.n_pairs)}</td>
                  <td>{num(d.p25, 3)}</td>
                  <td>{num(d.p50, 3)}</td>
                  <td>{num(d.p75, 3)}</td>
                  <td>{num(d.p75 - d.p25, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
      <Section
        title="Margin drivers (explanatory, not segments)"
        lede={s.margin_driver_note}
      >
        <RuleTable rules={s.margin_driver_rules} target="Mean pocket margin" />
      </Section>
    </div>
  );
}
