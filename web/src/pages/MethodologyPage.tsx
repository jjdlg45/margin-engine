/** Data & methodology: reconciliation, residual explanations, coverage,
 * captured warnings, the measured validation results table, and the link to
 * the full methodology document. */

import { PageHeader, Section } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import { useBundle } from "../lib/bundle";
import { count, dollars, pct } from "../lib/format";

const METHODOLOGY_URL = "https://github.com/jjdlg45/margin-engine/blob/main/docs/methodology.md";

export function MethodologyPage() {
  const b = useBundle();
  const k = b.kpis;
  return (
    <div>
      <PageHeader
        title="Data & methodology"
        lede={
          <>
            {b.data_label}. Every check below is measured by the engine's
            validation script - nothing is hand-typed. The full model detail
            lives in{" "}
            <a className="text-petrol underline-offset-4 hover:underline" href={METHODOLOGY_URL}>
              docs/methodology.md
            </a>
            .
          </>
        }
      />
      <Section title="Reconciliation">
        <div className="overflow-x-auto">
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Expected</th>
                <th>Actual</th>
                <th>Allocated</th>
                <th>Residual</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {b.reconciliation.map((c) => (
                <tr key={c.check}>
                  <td className="left">{c.check}</td>
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
        </div>
      </Section>
      {b.residuals.length > 0 && (
        <Section
          title="Visible residuals"
          lede="Dollars the pipeline keeps at pool level instead of forcing onto lines."
        >
          <div className="overflow-x-auto">
            <table>
              <thead>
                <tr>
                  <th>Residual</th>
                  <th>Amount</th>
                  <th>What it is</th>
                  <th>Why not allocated</th>
                  <th>Where listed</th>
                </tr>
              </thead>
              <tbody>
                {b.residuals.map((r) => (
                  <tr key={r.name}>
                    <td className="left font-mono text-caption">{r.name}</td>
                    <td>{dollars(r.amount_cents)}</td>
                    <td className="left">{r.what}</td>
                    <td className="left">{r.why_not_allocated}</td>
                    <td className="left">{r.where_listed}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
      <Section title="Coverage">
        <dl className="m-0 grid grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-4">
          <div>
            <dt className="text-caption secondary">Ledger lines</dt>
            <dd className="m-0 font-medium">{count(k.n_ledger_lines)}</dd>
          </div>
          <div>
            <dt className="text-caption secondary">Lines with effective list</dt>
            <dd className="m-0 font-medium">{pct(k.list_price_coverage_share)}</dd>
          </div>
          <div>
            <dt className="text-caption secondary">Revenue with missing cost</dt>
            <dd className="m-0 font-medium">{pct(k.missing_cost_revenue_share, 2)}</dd>
          </div>
          <div>
            <dt className="text-caption secondary">Window</dt>
            <dd className="m-0 font-medium">{k.window_label}</dd>
          </div>
        </dl>
      </Section>
      {b.library_warnings.length > 0 && (
        <Section
          title="Library warnings"
          lede="Captured during the run and recorded here and in the manifest - never printed and lost."
        >
          <div className="overflow-x-auto">
            <table className="max-w-3xl">
              <thead>
                <tr>
                  <th>Category</th>
                  <th>Message (counts normalized)</th>
                  <th>Occurrences</th>
                </tr>
              </thead>
              <tbody>
                {b.library_warnings.map((w, i) => (
                  <tr key={i}>
                    <td>{w.category}</td>
                    <td className="left">{w.message}</td>
                    <td>{count(w.n_occurrences)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
      <Section
        title="Validation results"
        lede="Planted truth vs recovered, measured by scripts/measure_validation.py against the same seeds and scales the gated test suite uses."
      >
        <div className="overflow-x-auto">
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Target</th>
                <th>Achieved</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {b.validation_results.map((v) => (
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
        </div>
      </Section>
    </div>
  );
}
