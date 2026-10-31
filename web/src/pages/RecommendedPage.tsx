/** Recommended actions: the start-here view. Open actions ranked by how sure
 * the engine is and then by expected impact, each leading with the decision
 * in plain words. Business numbers come from the engine's exports; this page
 * only orders and presents them. */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { QuerySection } from "../components/Async";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import { useLabels } from "../lib/labels";
import { useTableQuery } from "../lib/queries";
import { count, dollars } from "../lib/format";
import type { ActionRow } from "../types/bundle";

/** Confidence first, size second. Within a verdict the biggest expected
 * impact leads; a forecast-backed $5k outranks an unforecast $50k because the
 * question this page answers is "what should I do", not "what is largest". */
const VERDICT_RANK: Record<string, number> = {
  forecast_available: 0,
  upside_only: 1,
  too_little_evidence: 2,
};

const OPEN_STATUSES = new Set(["proposed", "under_review", "approved"]);

function rank(a: ActionRow): [number, number] {
  return [VERDICT_RANK[a.verdict ?? "upside_only"] ?? 3, -(a.expected_impact_cents ?? 0)];
}

function RecommendedCard({ a }: { a: ActionRow }) {
  const labels = useLabels();
  return (
    <li className="border-t hairline py-5 first:border-t-0">
      <Link to={`/actions/${a.action_id}`} className="block w-fit font-medium text-ink no-underline underline-offset-4 hover:underline">
        {a.title}
      </Link>
      <span className="mt-1.5 flex flex-wrap items-center gap-2">
        <StatusChip status={a.verdict ?? "upside_only"} />
        {a.staged_step && <StatusChip status="staged_first_step" />}
        <StatusChip status={a.status} />
        {a.compliance_flags && <StatusChip status="rp_review_flag" />}
      </span>
      <span className="mt-1.5 block text-sm">
        {a.expected_impact_cents !== null ? dollars(a.expected_impact_cents) : "No figure"}{" "}
        <span className="secondary">({labels(a.impact_basis).label.toLowerCase()})</span>
      </span>
      <span className="mt-0.5 block text-caption secondary">
        Owner: {a.owner}
        {a.due_date ? `. Due ${a.due_date}` : ""} ·{" "}
        <Link to={`/actions/${a.action_id}`} className="text-petrol underline underline-offset-4">
          Review and decide
        </Link>
      </span>
    </li>
  );
}

export function RecommendedPage() {
  const labels = useLabels();
  const [verdict, setVerdict] = useState<string | null>(null);
  const [shown, setShown] = useState(15);
  const table = useTableQuery("actions", { page: 1, page_size: 5000, filters: {} });

  const ranked = useMemo(() => {
    const rows = (table.data?.items ?? []) as ActionRow[];
    return rows
      .filter((a) => OPEN_STATUSES.has(a.status) && !a.blocked)
      .filter((a) => !verdict || (a.verdict ?? "upside_only") === verdict)
      .sort((x, y) => {
        const [xv, xi] = rank(x);
        const [yv, yi] = rank(y);
        return xv - yv || xi - yi;
      });
  }, [table.data, verdict]);

  return (
    <div>
      <PageHeader
        title="Recommended actions"
        lede="Start here. Every open action, ranked by how sure the engine is and then by expected impact - a forecast-backed move outranks a larger unforecast one. Each leads with the decision; the statistics behind it are one click away on the action itself."
      />
      <Section title="The queue, best-supported first">
        <div className="mb-4 flex flex-wrap items-center gap-2" data-testid="verdict-filter">
          <span className="text-caption secondary">Show:</span>
          {[null, "forecast_available", "upside_only", "too_little_evidence"].map((v) => (
            <button
              key={v ?? "all"}
              type="button"
              aria-pressed={verdict === v}
              onClick={() => setVerdict(v)}
              className={`rounded-badge border hairline px-2 py-1 text-caption ${
                verdict === v ? "bg-petrol text-paper" : "secondary hover:text-ink"
              }`}
            >
              {v ? labels(v).label : "All verdicts"}
            </button>
          ))}
        </div>
        <QuerySection query={table} label="recommended actions" lines={8}>
          {() =>
            ranked.length ? (
              <>
                <p className="m-0 text-caption secondary">
                  Showing {count(Math.min(shown, ranked.length))} of {count(ranked.length)} open
                  actions.
                </p>
                <ul className="m-0 list-none p-0" data-testid="recommended-list">
                  {ranked.slice(0, shown).map((a) => (
                    <RecommendedCard key={a.action_id} a={a} />
                  ))}
                </ul>
                {ranked.length > shown && (
                  <button
                    type="button"
                    className="mt-3 rounded-badge border hairline px-3 py-1.5 text-sm secondary hover:text-ink"
                    onClick={() => setShown(shown + 25)}
                  >
                    Show more
                  </button>
                )}
              </>
            ) : (
              <EmptyState title="Nothing waiting on a decision">
                Every generated action has been reviewed, or none met the guardrails. The full
                queue, including closed and blocked actions, is in the Action center.
              </EmptyState>
            )
          }
        </QuerySection>
        <p className="mb-0 mt-6 text-caption secondary">
          The full queue with filters, bulk review and the export lives in the{" "}
          <Link to="/actions" className="text-petrol underline underline-offset-4">
            Action center
          </Link>
          ; the three opportunity levels behind these figures are under{" "}
          <Link to="/opportunity" className="text-petrol underline underline-offset-4">
            Opportunity levels
          </Link>
          .
        </p>
      </Section>
    </div>
  );
}
