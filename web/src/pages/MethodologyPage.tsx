/** Data & methodology: reconciliation, residual explanations, coverage,
 * captured warnings, the measured validation results table, and the link to
 * the full methodology document. */

import { QuerySection } from "../components/Async";
import { PageHeader, Section, ScrollX } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import type { MethodologyData } from "../lib/api/types";
import { useAppData, usePageData } from "../lib/queries";
import { useLabels } from "../lib/labels";
import { count, dollars, pct } from "../lib/format";

const METHODOLOGY_URL = "https://github.com/jjdlg45/margin-engine/blob/main/docs/methodology.md";

function CoverageSection() {
  const app = useAppData();
  return (
    <Section title="Coverage">
      <QuerySection query={app} label="coverage" lines={2}>
        {(a) => (
          <dl className="m-0 grid grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-4">
            <div>
              <dt className="text-caption secondary">Ledger lines</dt>
              <dd className="m-0 font-medium">{count(a.kpis.n_ledger_lines)}</dd>
            </div>
            <div>
              <dt className="text-caption secondary">Lines with effective list</dt>
              <dd className="m-0 font-medium">{pct(a.kpis.list_price_coverage_share)}</dd>
            </div>
            <div>
              <dt className="text-caption secondary">Revenue with missing cost</dt>
              <dd className="m-0 font-medium">{pct(a.kpis.missing_cost_revenue_share, 2)}</dd>
            </div>
            <div>
              <dt className="text-caption secondary">Window</dt>
              <dd className="m-0 font-medium">{a.kpis.window_label}</dd>
            </div>
          </dl>
        )}
      </QuerySection>
    </Section>
  );
}

function MethodologyBody({ d }: { d: MethodologyData }) {
  const labels = useLabels();
  return (
    <>
      <Section title="Reconciliation">
        <ScrollX label="Methodology table">
          <table>
            <thead>
              <tr>
                <th className="left">Check</th>
                <th>Expected</th>
                <th>Actual</th>
                <th>Allocated</th>
                <th className="left">Residual</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {d.reconciliation.map((c) => (
                <tr key={c.check}>
                  <td className="left" title={c.check}>
                    {labels(c.check).label}
                  </td>
                  <td>{dollars(c.expected_cents)}</td>
                  <td>{dollars(c.actual_cents)}</td>
                  <td>{c.allocated_cents !== null && c.allocated_cents !== undefined ? dollars(c.allocated_cents) : "—"}</td>
                  <td>{c.residual_cents !== null && c.residual_cents !== undefined ? dollars(c.residual_cents) : "—"}</td>
                  <td>
                    <StatusChip status={c.passed ? "reconciled" : "reconciliation_failed"} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollX>
      </Section>
      {d.residuals.length > 0 && (
        <Section
          title="Visible residuals"
          lede="Dollars the pipeline keeps at pool level instead of forcing onto lines."
        >
          <ScrollX label="Methodology table">
            <table>
              <thead>
                <tr>
                  <th className="left">Residual</th>
                  <th>Amount</th>
                  <th className="left">What it is</th>
                  <th className="left">Why not allocated</th>
                  <th className="left">Where listed</th>
                </tr>
              </thead>
              <tbody>
                {d.residuals.map((r) => (
                  <tr key={r.name}>
                    <td className="left">
                      {labels(r.name).label}
                      <details className="text-caption secondary">
                        <summary>Technical key</summary>
                        <code>{r.name}</code>
                      </details>
                    </td>
                    <td>{dollars(r.amount_cents)}</td>
                    <td className="left">{r.what}</td>
                    <td className="left">{r.why_not_allocated}</td>
                    <td className="left">{r.where_listed}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollX>
        </Section>
      )}
      <CoverageSection />
      {d.library_warnings.length > 0 && (
        <Section
          title="Library warnings"
          lede="Captured during the run and recorded here and in the manifest - never printed and lost."
        >
          <ScrollX label="Methodology table">
            <table className="max-w-3xl">
              <thead>
                <tr>
                  <th>Category</th>
                  <th className="left">Message (counts normalized)</th>
                  <th>Occurrences</th>
                </tr>
              </thead>
              <tbody>
                {d.library_warnings.map((w, i) => (
                  <tr key={i}>
                    <td>{w.category}</td>
                    <td className="left">{w.message}</td>
                    <td>{count(w.n_occurrences)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollX>
        </Section>
      )}
      <Section
        title="Validation results"
        lede="Planted truth vs recovered, measured by scripts/measure_validation.py against the same seeds and scales the gated test suite uses."
      >
        <ScrollX label="Methodology table">
          <table>
            <thead>
              <tr>
                <th className="left">Check</th>
                <th className="left">Target</th>
                <th className="left">Achieved</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {d.validation_results.map((v) => (
                <tr key={v.check}>
                  <td className="left">{v.check}</td>
                  <td className="left">{v.target}</td>
                  <td className="left">{v.achieved}</td>
                  <td>
                    <StatusChip status={v.passed ? "reconciled" : "reconciliation_failed"} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollX>
      </Section>
    </>
  );
}

export function MethodologyPage() {
  const q = usePageData("methodology");
  return (
    <div>
      <PageHeader
        title="Data & methodology"
        lede={
          <>
            {q.data?.data_label ?? "Synthetic demo data"}. Every check below is
            measured by the engine's validation script - nothing is hand-typed.
            The full model detail lives in{" "}
            <a className="text-petrol underline underline-offset-4" href={METHODOLOGY_URL}>
              docs/methodology.md
            </a>
            .
          </>
        }
      />
      <QuerySection query={q} label="methodology checks" lines={8}>
        {(d) => <MethodologyBody d={d} />}
      </QuerySection>
    </div>
  );
}
