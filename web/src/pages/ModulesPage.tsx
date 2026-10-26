/** Optional modules: win curves, cross-price, inventory costing. Each shows
 * only when present in the bundle; absence states its reason in plain
 * language rather than hiding the section. */

import { QuerySection } from "../components/Async";
import { EmptyState, PageHeader, Section, ScrollX } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import type { OptionalModules } from "../types/bundle";
import { usePageData } from "../lib/queries";
import { count, dollars, num, pct } from "../lib/format";

function ModulesBody({ m }: { m: OptionalModules }) {
  return (
    <>
      <Section
        title="Quote win/loss curves"
        lede="Logistic win probability on the quote price index, per segment. Only precision-gated fits carry estimated status; the rest are reported with their reason and never gated."
      >
        {m.win_curves ? (
          <ScrollX label="Module table">
            <table>
              <thead>
                <tr>
                  <th>Segment</th>
                  <th>Quotes</th>
                  <th>Win rate</th>
                  <th>Price coefficient</th>
                  <th>95% CI</th>
                  <th>Negotiation gap</th>
                  <th className="left">Status</th>
                </tr>
              </thead>
              <tbody>
                {m.win_curves.map((w) => (
                  <tr key={w.segment}>
                    <td>{w.segment}</td>
                    <td>{count(w.n_quotes)}</td>
                    <td>{pct(w.win_rate)}</td>
                    <td>{w.price_coef !== null ? num(w.price_coef) : "—"}</td>
                    <td>
                      {w.ci_low !== null && w.ci_high !== null
                        ? `[${num(w.ci_low)}, ${num(w.ci_high)}]`
                        : "—"}
                    </td>
                    <td>{w.negotiation_gap !== null ? pct(w.negotiation_gap) : "—"}</td>
                    <td>
                      <StatusChip status={w.status} reason={w.reasons || undefined} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollX>
        ) : (
          <EmptyState title="Win/loss modeling is off">{m.win_curves_absent_reason}</EmptyState>
        )}
      </Section>
      <Section
        title="Cross-price effects"
        lede="Estimated only within declared substitution groups and only where identified; everything else is labeled, not estimated."
      >
        {m.cross_price ? (
          <ScrollX label="Module table">
            <table>
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Item</th>
                  <th>Substitute</th>
                  <th>Observations</th>
                  <th>Cross elasticity</th>
                  <th>Instrument strength for this price alone</th>
                  <th className="left">Status</th>
                </tr>
              </thead>
              <tbody>
                {m.cross_price.map((c) => (
                  <tr key={`${c.group_id}-${c.item_a}-${c.item_b}`}>
                    <td>{c.group_id}</td>
                    <td className="font-mono">{c.item_a}</td>
                    <td className="font-mono">{c.item_b}</td>
                    <td>{count(c.n_obs)}</td>
                    <td>{c.cross_elasticity !== null ? num(c.cross_elasticity) : "—"}</td>
                    <td title={c.first_stage_detail || undefined}>
                      {c.conditional_effective_f !== null && c.conditional_effective_f !== undefined
                        ? num(c.conditional_effective_f, 1)
                        : "—"}
                      {c.marginal_first_stage_f !== null && c.marginal_first_stage_f !== undefined
                        ? ` (${num(c.marginal_first_stage_f, 1)} without holding the other price fixed)`
                        : ""}
                    </td>
                    <td>
                      <StatusChip status={c.status} reason={c.reasons || undefined} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollX>
        ) : (
          <EmptyState title="Cross-price estimation is off">{m.cross_price_absent_reason}</EmptyState>
        )}
      </Section>
      <Section
        title="Inventory costing"
        lede="Receipts-based COGS under the mapped data's declared cost basis, with the timing statement tariff scenarios use."
      >
        {m.inventory ? (
          <dl className="m-0 grid max-w-2xl grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-4">
            <div>
              <dt className="text-caption secondary">Method</dt>
              <dd className="m-0 font-medium">{m.inventory.method}</dd>
            </div>
            <div>
              <dt className="text-caption secondary">Sales costed</dt>
              <dd className="m-0 font-medium">{count(m.inventory.n_sales_costed)}</dd>
            </div>
            <div>
              <dt className="text-caption secondary">COGS total</dt>
              <dd className="m-0 font-medium">{dollars(m.inventory.cogs_total_cents)}</dd>
            </div>
            <div>
              <dt className="text-caption secondary">Flagged lines</dt>
              <dd className="m-0 font-medium">{count(m.inventory.n_flagged)}</dd>
            </div>
            <div className="col-span-2 sm:col-span-4">
              <dt className="text-caption secondary">Timing</dt>
              <dd className="m-0">{m.inventory.timing_statement}</dd>
            </div>
          </dl>
        ) : (
          <EmptyState title="Inventory costing is off">{m.inventory_absent_reason}</EmptyState>
        )}
      </Section>
    </>
  );
}

export function ModulesPage() {
  const q = usePageData("modules");
  return (
    <div>
      <PageHeader
        title="Optional modules"
        lede="Each module switches on only when its input data exists in the mapped export; when one is off, the reason is stated here."
      />
      <QuerySection query={q} label="optional modules" lines={8}>
        {(m) => <ModulesBody m={m} />}
      </QuerySection>
    </div>
  );
}
