/** Price sensitivity: per-segment elasticity with intervals and evidence
 * status, the naive-vs-corrected comparison, and expandable diagnostics
 * (first stage, overidentification, robustness, identifying coverage). */

import * as Collapsible from "@radix-ui/react-collapsible";
import { IntervalChart } from "../charts/Interval";
import { QuerySection } from "../components/Async";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import type { SensitivityData } from "../lib/api/types";
import { useLabels } from "../lib/labels";
import { usePageData } from "../lib/queries";
import { count, num, pct } from "../lib/format";

function SensitivityBody({ d }: { d: SensitivityData }) {
  const labels = useLabels();
  const rankOf = new Map(d.segment_names.map((r) => [r.leaf_id, r.display_rank]));
  const disp = (seg: number) => `Segment ${rankOf.get(seg) ?? seg}`;
  const rows = [...d.rows].sort(
    (a, z) => (rankOf.get(a.segment) ?? 99) - (rankOf.get(z.segment) ?? 99),
  );
  const withIv = rows.filter((r) => r.iv !== null);
  return (
    <>
      {withIv.length ? (
        <div className="panel mt-2 px-5 py-6 sm:px-8">
          <IntervalChart rows={rows} nameOf={disp} />
          <p className="mb-0 mt-3 text-caption secondary">
            Filled circle = causal-supported; half-filled = IV-level; open =
            descriptive. The open amber square is the naive within-estimate:
            reps discount when demand is weak, which biases naive estimates
            toward zero, so the corrected estimates sit further from zero.
          </p>
        </div>
      ) : (
        <EmptyState title="No instrumented estimates">
          No segment had enough identifying variation for an IV estimate in
          this run; every segment is capped at descriptive or insufficient
          data and excluded from volume-response math.
        </EmptyState>
      )}
      <Section title="Per-segment detail">
        <ul className="m-0 list-none p-0">
          {rows.map((r) => (
            <li key={r.segment} className="border-t hairline py-4">
              <Collapsible.Root>
                <div className="row items-center">
                  <span className="w-28 font-medium">{disp(r.segment)}</span>
                  <span className="w-24">{r.iv !== null ? num(r.iv) : "—"}</span>
                  <span className="text-caption secondary">
                    {r.iv_ci_low !== null && r.iv_ci_high !== null
                      ? `95% CI [${num(r.iv_ci_low)}, ${num(r.iv_ci_high)}]`
                      : "no interval"}
                  </span>
                  <StatusChip status={r.status} reason={r.status_reasons || undefined} />
                  <Collapsible.Trigger className="ml-auto cursor-pointer rounded-badge border hairline bg-transparent px-2 py-1 text-caption secondary hover:text-ink">
                    Diagnostics
                  </Collapsible.Trigger>
                </div>
                <Collapsible.Content>
                  <dl className="m-0 mt-3 grid grid-cols-2 gap-x-8 gap-y-2 rounded-[8px] bg-inset/60 px-4 py-3 sm:grid-cols-4">
                    <div>
                      <dt className="text-caption secondary">Observations / pairs</dt>
                      <dd className="m-0">
                        {count(r.n_obs)} / {count(r.n_pairs)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">
                        First-stage F (before controlling)
                      </dt>
                      <dd className="m-0">
                        {r.first_stage_f !== null ? num(r.first_stage_f, 1) : "—"}
                        {r.first_stage_f_uncontrolled !== null &&
                        r.first_stage_f_uncontrolled !== undefined
                          ? ` (${num(r.first_stage_f_uncontrolled, 1)})`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Overidentification p</dt>
                      <dd className="m-0">{r.overid_p !== null ? num(r.overid_p, 3) : "—"}</dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Robustness spread</dt>
                      <dd className="m-0">
                        {r.robustness_spread !== null ? num(r.robustness_spread, 3) : "—"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Identifying coverage</dt>
                      <dd className="m-0">
                        {r.identifying_coverage !== null ? num(r.identifying_coverage, 2) : "—"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Anderson-Rubin set</dt>
                      <dd className="m-0">
                        {r.ar_ci_low !== null && r.ar_ci_high !== null
                          ? `[${num(r.ar_ci_low)}, ${num(r.ar_ci_high)}]`
                          : "empty / not applicable"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Naive (biased) estimate</dt>
                      <dd className="m-0">{r.naive_fe_ols !== null ? num(r.naive_fe_ols) : "—"}</dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">PPML (with zeros / positive only)</dt>
                      <dd className="m-0">
                        {r.ppml !== null && r.ppml !== undefined ? num(r.ppml) : "—"}
                        {" / "}
                        {r.ppml_positive !== null && r.ppml_positive !== undefined
                          ? num(r.ppml_positive)
                          : "—"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Carried-forward rows</dt>
                      <dd className="m-0">
                        {r.carried_forward_share !== null && r.carried_forward_share !== undefined
                          ? pct(r.carried_forward_share)
                          : "—"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Identified price range</dt>
                      <dd className="m-0">
                        {r.support_log_lo !== null &&
                        r.support_log_lo !== undefined &&
                        r.support_log_hi !== null &&
                        r.support_log_hi !== undefined
                          ? `${(100 * (Math.exp(r.support_log_lo) - 1)).toFixed(1)}% to +${(
                              100 *
                              (Math.exp(r.support_log_hi) - 1)
                            ).toFixed(1)}%`
                          : "—"}
                      </dd>
                    </div>
                  </dl>
                  {/* Competitor-price path and the per-gate checklist (Run 6,
                      Phase 70). Pass/fail/not-evaluated is carried by a glyph
                      AND words, never colour alone. */}
                  {(r.estimand || r.market_control_status) && (
                    <dl className="m-0 mt-3 grid grid-cols-1 gap-x-8 gap-y-2 rounded-[8px] bg-inset/60 px-4 py-3 sm:grid-cols-2">
                      {r.estimand && (
                        <div>
                          <dt className="text-caption secondary">What this measures</dt>
                          <dd className="m-0">
                            {labels(r.estimand).label}
                            {r.estimand_bias_direction ? (
                              <span className="secondary">
                                {" — "}
                                {labels(r.estimand_bias_direction).description}
                              </span>
                            ) : null}
                          </dd>
                        </div>
                      )}
                      {r.market_control_status && (
                        <div>
                          <dt className="text-caption secondary">Market price index</dt>
                          <dd className="m-0">
                            {labels(r.market_control_status).label}
                            {r.market_control_price_r2 !== null &&
                            r.market_control_price_r2 !== undefined ? (
                              <span className="secondary">
                                {" — explains "}
                                {pct(r.market_control_price_r2)}
                                {" of price movement"}
                              </span>
                            ) : null}
                          </dd>
                        </div>
                      )}
                    </dl>
                  )}
                  {(r.causal_gates ?? []).length > 0 && (
                    <ul
                      className="m-0 mt-3 list-none p-0 text-caption"
                      data-testid="causal-gates"
                    >
                      {(r.causal_gates ?? []).map((g) => (
                        <li key={g.gate} className="flex gap-2 py-0.5">
                          <span aria-hidden="true" className="w-4 shrink-0">
                            {g.result === "pass" ? "✓" : g.result === "fail" ? "✕" : "–"}
                          </span>
                          <span>
                            <span className="font-medium">{labels(g.gate).label}</span>
                            {": "}
                            <span className="secondary">{labels(g.result).label}</span>
                            {g.detail ? <span className="secondary"> — {g.detail}</span> : null}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <p className="mb-0 mt-2 max-w-2xl text-caption secondary">{r.naive_vs_iv_note}</p>
                </Collapsible.Content>
              </Collapsible.Root>
            </li>
          ))}
        </ul>
      </Section>
    </>
  );
}

export function SensitivityPage() {
  const q = usePageData("sensitivity");
  return (
    <div>
      <PageHeader
        title="Price sensitivity"
        lede={
          <>
            Own-price elasticity per segment, instrumented with supplier cost
            and category tariff shocks. The evidence status is the headline:
            only causal-supported estimates feed volume-response math anywhere
            in this app. Open a segment for its full diagnostics.
          </>
        }
      />
      <QuerySection query={q} label="price sensitivity" lines={8}>
        {(d) => <SensitivityBody d={d} />}
      </QuerySection>
    </div>
  );
}
