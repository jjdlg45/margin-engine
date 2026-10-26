/** Overview: headline outcome, reconciliation status, the waterfall as the
 * signature visual, ranked key findings linking to detail pages, and the data
 * coverage band. Every figure is taken from the engine's exports; filtering
 * and counting is the only client-side arithmetic. Loads only the app and
 * overview payloads. */

import { Link } from "react-router-dom";
import { Waterfall } from "../charts/Waterfall";
import { QuerySection } from "../components/Async";
import { StatusChip } from "../components/StatusChip";
import type { AppData, OverviewData } from "../lib/api/types";
import { useAppData, usePageData } from "../lib/queries";
import { useCompact } from "../lib/useCompact";
import { count, dollars, dollarsShort, pct } from "../lib/format";

interface Finding {
  figure: string;
  text: string;
  to: string;
  linkLabel: string;
}

function findings(o: OverviewData): Finding[] {
  const out: Finding[] = [];
  const topLeak = o.leakage_top_component;
  out.push({
    figure: dollarsShort(o.leakage_total_cents),
    text: `of pricing leakage between list and pocket over the ${o.window_label}${
      topLeak ? `, led by ${topLeak.component.toLowerCase()} at ${pct(topLeak.share_of_leakage)}` : ""
    }.`,
    to: "/waterfall",
    linkLabel: "Price waterfall",
  });
  out.push({
    figure: dollarsShort(o.gross_total_cents),
    text: `gross price opportunity over the ${o.window_label} at unchanged volume - a different question from leakage, never added to it.`,
    to: "/opportunity",
    linkLabel: "Opportunity",
  });
  if (o.below_floor_pairs > 0) {
    out.push({
      figure: count(o.below_floor_pairs),
      text: "customer-item pairs are priced below the economic floor that guarantees the minimum pocket margin.",
      to: "/guidance",
      linkLabel: "Price guidance",
    });
  }
  out.push({
    figure: `${o.causal_segments} of ${o.total_segments}`,
    text: "segments reach causal-supported elasticity; the rest are labeled and excluded from volume-response math - the engine fails closed.",
    to: "/sensitivity",
    linkLabel: "Price sensitivity",
  });
  if (o.negative_contribution_orders > 0) {
    out.push({
      figure: count(o.negative_contribution_orders),
      text: "orders lose money after cost-to-serve - a service-policy question kept separate from pricing.",
      to: "/customers",
      linkLabel: "Customers & orders",
    });
  }
  if (o.open_actions && o.open_actions.n_actions > 0) {
    const open =
      o.open_actions.n_actions -
      (o.open_actions.by_status.rejected ?? 0) -
      (o.open_actions.by_status.measured ?? 0);
    out.push({
      figure: count(open),
      text: `open actions await review or implementation${
        o.open_actions.n_measured > 0
          ? `; ${count(o.open_actions.n_measured)} already measured against untreated comparison pairs`
          : ""
      }.`,
      to: "/actions",
      linkLabel: "Action center",
    });
  }
  return out.slice(0, 5);
}

function Headline({ app }: { app: AppData }) {
  const k = app.kpis;
  return (
    <div className="flex flex-wrap items-baseline gap-x-12 gap-y-5">
      <div>
        <span className="block text-display font-bold tracking-tight text-petrol">{dollarsShort(k.net_revenue_cents)}</span>
        <span className="text-caption secondary">net invoiced revenue after returns, {k.window_label}</span>
      </div>
      <div>
        <span className="block text-display font-bold tracking-tight">{dollarsShort(k.pocket_revenue_cents)}</span>
        <span className="text-caption secondary">pocket revenue after rebates & concessions, {k.window_label}</span>
      </div>
      <div>
        <span className="block text-display font-bold tracking-tight">{pct(k.pocket_margin_pct)}</span>
        <span className="text-caption secondary">pocket gross margin, cost-known sale lines, {k.window_label}</span>
      </div>
      <div className="self-center">
        {k.reconciliation_pass ? (
          <StatusChip status="reconciled" />
        ) : (
          <StatusChip status="reconciliation_failed" />
        )}
        <span className="ml-2 text-caption secondary">to the cent, every check</span>
        <span className="block text-caption secondary">
          {(k.n_visible_residuals ?? 0) > 0
            ? `${k.n_visible_residuals} visible residual${(k.n_visible_residuals ?? 0) === 1 ? "" : "s"} (${dollarsShort(k.visible_residual_cents ?? 0)}) explained, never forced onto lines`
            : "no residuals: every allocated amount reached its lines"}
          {"; cost known on "}
          {pct(1 - k.missing_cost_revenue_share)} of revenue
        </span>
      </div>
    </div>
  );
}

export function Overview() {
  const app = useAppData();
  const overview = usePageData("overview");
  const compact = useCompact();
  return (
    <div>
      {/* Headline band */}
      <section aria-labelledby="headline" className="pb-14 pt-12">
        <h1 id="headline" className="heading-rule m-0 max-w-2xl text-display">
          Every source dollar reconciled or explained.
        </h1>
        <p className="mb-8 mt-3 max-w-2xl secondary">
          An auditable pricing engine for B2B distributors: it reconciles the
          path from list price to pocket margin to the cent, and labels how
          much evidence stands behind every number it reports.
        </p>
        {/* Reserved heights measured from the rendered content so the
            skeleton-to-content swap causes no layout shift (Lighthouse CLS). */}
        <QuerySection query={app} label="headline figures" lines={2} reserve="sm:min-h-[109px]">
          {(a) => <Headline app={a} />}
        </QuerySection>
      </section>

      {/* Waterfall band */}
      <section aria-labelledby="wf" className="panel px-5 pb-5 pt-8 sm:px-8">
        <h2 id="wf" className="heading-rule mb-2 mt-0 text-section">
          The price waterfall
        </h2>
        <p className="mb-6 mt-0 max-w-2xl text-caption secondary">
          Sale lines only - returns appear in Customers & orders. Hatched bars are pricing leakage (discounts, rebates, concessions); dotted bars are costs. Hover any bar for exact amounts and the share of lines covered at that stage.
        </p>
        {/* The chart's height is a constant (360 compact / 264 desktop,
            matching the 640px breakpoint), so the reservation is exact. */}
        <QuerySection
          query={overview}
          label="the price waterfall"
          lines={6}
          reserve="min-h-[360px] sm:min-h-[264px]"
        >
          {(o) => <Waterfall stages={o.waterfall_overall} compact={compact} />}
        </QuerySection>
      </section>

      {/* Key findings */}
      <section aria-labelledby="findings" className="py-14">
        <h2 id="findings" className="heading-rule mb-5 mt-0 text-section">
          What the engine found
        </h2>
        {/* Floor at the three findings every dataset produces (~81px each);
            more findings may extend below the reservation. */}
        <QuerySection query={overview} label="key findings" lines={5} reserve="sm:min-h-[242px]">
          {(o) => (
            <ul className="m-0 list-none p-0">
              {findings(o).map((f) => (
                <li key={f.to} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-t hairline py-5 sm:flex-nowrap">
                  <span className="w-28 shrink-0 text-title font-semibold text-petrol">{f.figure}</span>
                  <span className="min-w-0 grow">{f.text}</span>
                  <Link to={f.to} className="shrink-0 font-medium text-petrol underline-offset-4 hover:underline">
                    {f.linkLabel}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </QuerySection>
      </section>

      {/* Coverage band */}
      <section aria-labelledby="coverage" className="mt-2 rounded-[8px] bg-inset/60 px-5 py-8 sm:px-8">
        <h2 id="coverage" className="heading-rule mb-5 mt-0 text-section">
          Data coverage
        </h2>
        <QuerySection query={app} label="data coverage" lines={3}>
          {(a) => {
            const k = a.kpis;
            return (
              <>
                <dl className="m-0 grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3 lg:grid-cols-6">
                  <div>
                    <dt className="text-caption secondary">Ledger lines</dt>
                    <dd className="m-0 font-medium">{count(k.n_ledger_lines)}</dd>
                  </div>
                  <div>
                    <dt className="text-caption secondary">Customers / items</dt>
                    <dd className="m-0 font-medium">
                      {count(k.n_customers)} / {count(k.n_items)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-caption secondary">Months covered</dt>
                    <dd className="m-0 font-medium">{count(k.months_covered)}</dd>
                  </div>
                  <div>
                    <dt className="text-caption secondary">Lines with effective list</dt>
                    <dd className="m-0 font-medium">{pct(k.list_price_coverage_share)}</dd>
                  </div>
                  <div>
                    <dt className="text-caption secondary">Revenue with missing cost</dt>
                    <dd className="m-0 font-medium">
                      {pct(k.missing_cost_revenue_share, 2)}
                      <span className="est-marker" aria-hidden="true" />
                      <span className="sr-only">excluded from cost-dependent metrics</span>
                    </dd>
                  </div>
                  <div>
                    <dt className="text-caption secondary">Library warnings captured</dt>
                    <dd className="m-0 font-medium">
                      <QuerySection query={overview} label="library warnings" lines={1}>
                        {(o) => <>{count(o.n_library_warnings)}</>}
                      </QuerySection>
                    </dd>
                  </div>
                </dl>
                <p className="mb-0 mt-5 text-caption secondary">
                  {dollars(k.contribution_cents)} of contribution on cost-known
                  sale lines over {k.window_label}; returns of {dollars(k.returns_cents)} are
                  included in net invoiced revenue and customer economics. Lines without cost stay in revenue figures and are excluded
                  from margin figures - the exclusion is reported, never hidden.
                </p>
              </>
            );
          }}
        </QuerySection>
      </section>
    </div>
  );
}
