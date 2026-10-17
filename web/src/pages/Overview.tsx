/** Overview: headline outcome, reconciliation status, the waterfall as the
 * signature visual, ranked key findings linking to detail pages, and the data
 * coverage band. Every figure is taken from the bundle; filtering and counting
 * is the only client-side arithmetic. */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Waterfall } from "../charts/Waterfall";
import { StatusChip } from "../components/StatusChip";
import { useBundle } from "../lib/bundle";
import { count, dollars, dollarsShort, pct } from "../lib/format";

function useCompact(): boolean {
  const [compact, setCompact] = useState(
    () => typeof window !== "undefined" && window.innerWidth < 640,
  );
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 639px)");
    const onChange = () => setCompact(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return compact;
}

interface Finding {
  figure: string;
  text: string;
  to: string;
  linkLabel: string;
}

function findings(b: ReturnType<typeof useBundle>): Finding[] {
  const out: Finding[] = [];
  const topLeak = [...b.leakage_components_overall].sort(
    (x, y) => y.amount_cents - x.amount_cents,
  )[0];
  out.push({
    figure: dollarsShort(b.opportunity.leakage_total_cents),
    text: `of pricing leakage between list and pocket over the ${b.opportunity.window_label}${
      topLeak ? `, led by ${topLeak.component.toLowerCase()} at ${pct(topLeak.share_of_leakage)}` : ""
    }.`,
    to: "/waterfall",
    linkLabel: "Price waterfall",
  });
  out.push({
    figure: dollarsShort(b.opportunity.gross_total_cents),
    text: `gross price opportunity over the ${b.opportunity.window_label} at unchanged volume - a different question from leakage, never added to it.`,
    to: "/opportunity",
    linkLabel: "Opportunity",
  });
  const belowFloor = b.guidance.filter((g) => g.status === "below_floor").length;
  if (belowFloor > 0) {
    out.push({
      figure: count(belowFloor),
      text: "customer-item pairs are priced below the economic floor that guarantees the minimum pocket margin.",
      to: "/guidance",
      linkLabel: "Price guidance",
    });
  }
  const causal = b.sensitivity.filter((s) => s.status === "causal_elasticity_supported").length;
  out.push({
    figure: `${causal} of ${b.sensitivity.length}`,
    text: "segments reach causal-supported elasticity; the rest are labeled and excluded from volume-response math - the engine fails closed.",
    to: "/sensitivity",
    linkLabel: "Price sensitivity",
  });
  if (b.economics.orders.negative_contribution_orders > 0) {
    out.push({
      figure: count(b.economics.orders.negative_contribution_orders),
      text: "orders lose money after cost-to-serve - a service-policy question kept separate from pricing.",
      to: "/customers",
      linkLabel: "Customers & orders",
    });
  }
  return out.slice(0, 5);
}

export function Overview() {
  const b = useBundle();
  const compact = useCompact();
  const k = b.kpis;
  return (
    <div>
      {/* Headline band */}
      <section aria-labelledby="headline" className="pb-14 pt-12">
        <h1 id="headline" className="heading-rule m-0 max-w-2xl text-display">
          Every dollar accounted for.
        </h1>
        <p className="mb-8 mt-3 max-w-2xl secondary">
          An auditable pricing engine for B2B distributors: it reconciles the
          path from list price to pocket margin to the cent, and labels how
          much evidence stands behind every number it reports.
        </p>
        <div className="flex flex-wrap items-baseline gap-x-12 gap-y-5">
          <div>
            <span className="block text-display font-bold tracking-tight text-petrol">{dollarsShort(k.net_revenue_cents)}</span>
            <span className="text-caption secondary">net revenue after returns, {k.window_label}</span>
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
          </div>
        </div>
      </section>

      {/* Waterfall band */}
      <section aria-labelledby="wf" className="panel px-5 py-8 sm:px-8">
        <h2 id="wf" className="heading-rule mb-2 mt-0 text-section">
          The price waterfall
        </h2>
        <p className="mb-6 mt-0 max-w-2xl text-caption secondary">
          List value to contribution. Hatched bars are pricing leakage (discounts, rebates, concessions); dotted bars are costs. Hover any bar for exact amounts and the share of lines covered at that stage.
        </p>
        <Waterfall stages={b.waterfall_overall} compact={compact} />
      </section>

      {/* Key findings */}
      <section aria-labelledby="findings" className="py-14">
        <h2 id="findings" className="heading-rule mb-5 mt-0 text-section">
          What the engine found
        </h2>
        <ul className="m-0 list-none p-0">
          {findings(b).map((f) => (
            <li key={f.to} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-t hairline py-5 sm:flex-nowrap">
              <span className="w-28 shrink-0 text-title font-semibold text-petrol">{f.figure}</span>
              <span className="min-w-0 grow">{f.text}</span>
              <Link to={f.to} className="shrink-0 font-medium text-petrol underline-offset-4 hover:underline">
                {f.linkLabel}
              </Link>
            </li>
          ))}
        </ul>
      </section>

      {/* Coverage band */}
      <section aria-labelledby="coverage" className="mt-2 rounded-[8px] bg-inset/60 px-5 py-8 sm:px-8">
        <h2 id="coverage" className="heading-rule mb-5 mt-0 text-section">
          Data coverage
        </h2>
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
            <dd className="m-0 font-medium">{count(b.library_warnings.length)}</dd>
          </div>
        </dl>
        <p className="mb-0 mt-5 text-caption secondary">
          {dollars(b.kpis.contribution_cents)} of contribution on cost-known
          sale lines over {k.window_label}; returns of {dollars(k.returns_cents)} are
          included in net revenue and customer economics. Lines without cost stay in revenue figures and are excluded
          from margin figures - the exclusion is reported, never hidden.
        </p>
      </section>
    </div>
  );
}
