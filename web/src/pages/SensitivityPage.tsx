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
        {/* What these two diagnostics can and cannot establish, stated once and
            always visible rather than inside a panel a reader has to open
            (Run 6, Phase 71). The same text is the tooltip on each figure. */}
        <dl className="m-0 mb-4 grid gap-y-2">
          <div>
            <dt className="text-caption font-medium">
              {labels("overidentification_blind_spot").label}
            </dt>
            <dd className="m-0 max-w-2xl text-caption secondary">
              {labels("overidentification_blind_spot").description}
            </dd>
          </div>
          <div>
            <dt className="text-caption font-medium">
              {labels("anderson_rubin_blind_spot").label}
            </dt>
            <dd className="m-0 max-w-2xl text-caption secondary">
              {labels("anderson_rubin_blind_spot").description}
            </dd>
          </div>
        </dl>
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
                      <dt
                        className="text-caption secondary"
                        title={labels("overidentification_blind_spot").description}
                      >
                        Overidentification p
                      </dt>
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
                      <dt className="text-caption secondary">
                        Robust effective F (published bar 23.1)
                      </dt>
                      <dd className="m-0">
                        <span title={r.effective_f_detail || undefined}>
                          {r.effective_first_stage_f !== null &&
                          r.effective_first_stage_f !== undefined
                            ? num(r.effective_first_stage_f, 1)
                            : "—"}
                        </span>
                        {r.n_clusters !== null && r.n_clusters !== undefined
                          ? ` over ${count(r.n_clusters)} clusters`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      {/* Cost and tariff movements are assigned to whole
                          suppliers and categories, so those are the units that
                          count as independent - not the number of lines. */}
                      <dt className="text-caption secondary">Independent groups</dt>
                      <dd className="m-0">
                        <span title={r.cluster_variation_detail || undefined}>
                          {r.cluster_level_used
                            ? labels(`cluster_${r.cluster_level_used}`).label
                            : "—"}
                        </span>
                        {r.n_clusters !== null && r.n_clusters !== undefined
                          ? `, ${count(r.n_clusters)} groups`
                          : ""}
                        {r.effective_clusters !== null && r.effective_clusters !== undefined
                          ? ` (${num(r.effective_clusters, 1)} weighted by how much each moves)`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">How the interval was built</dt>
                      <dd className="m-0">
                        <span
                          title={
                            r.small_cluster_detail ||
                            (r.primary_interval_basis
                              ? labels(r.primary_interval_basis).description
                              : undefined)
                          }
                        >
                          {r.primary_interval_basis
                            ? labels(r.primary_interval_basis).label
                            : "—"}
                        </span>
                        {r.wild_bootstrap_reps_used !== null &&
                        r.wild_bootstrap_reps_used !== undefined
                          ? `, ${count(r.wild_bootstrap_reps_used)} resamples`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      {/* The extent of the robust set is a HULL, not a
                          confidence interval - named as one. */}
                      <dt
                        className="text-caption secondary"
                        title={labels("anderson_rubin_blind_spot").description}
                      >
                        Weak-instrument-robust set
                      </dt>
                      <dd className="m-0">
                        <span title={r.ar_shape ? labels(r.ar_shape).description : undefined}>
                          {r.ar_shape ? labels(r.ar_shape).label : "—"}
                        </span>
                        {r.ar_hull_lo !== null &&
                        r.ar_hull_lo !== undefined &&
                        r.ar_hull_hi !== null &&
                        r.ar_hull_hi !== undefined
                          ? `, hull [${num(r.ar_hull_lo)}, ${num(r.ar_hull_hi)}]`
                          : ""}
                        {r.ar_n_components !== null &&
                        r.ar_n_components !== undefined &&
                        r.ar_n_components > 1
                          ? ` in ${count(r.ar_n_components)} parts`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Placebo check</dt>
                      <dd className="m-0">
                        <span
                          title={
                            r.placebo_detail ||
                            (r.placebo_status ? labels(r.placebo_status).description : undefined)
                          }
                        >
                          {r.placebo_status ? labels(r.placebo_status).label : "—"}
                        </span>
                        {r.placebo_share_significant !== null &&
                        r.placebo_share_significant !== undefined
                          ? ` (${pct(r.placebo_share_significant)} of shuffled draws significant)`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Anticipation test</dt>
                      <dd className="m-0">
                        <span
                          title={
                            r.lead_test_detail ||
                            (r.lead_test_status ? labels(r.lead_test_status).description : undefined)
                          }
                        >
                          {r.lead_test_status ? labels(r.lead_test_status).label : "—"}
                        </span>
                        {r.lead_test_bound !== null && r.lead_test_bound !== undefined
                          ? ` (detects effects above ${num(r.lead_test_bound)})`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      {/* The SECOND estimand, named for what it measures. It is
                          reported beside the headline, never above it. */}
                      <dt
                        className="text-caption secondary"
                        title={r.iv_ppml_reasons || undefined}
                      >
                        Total quantity, including whether they buy at all
                      </dt>
                      <dd className="m-0">
                        {r.iv_ppml !== null && r.iv_ppml !== undefined ? num(r.iv_ppml) : "—"}
                        {r.iv_ppml_status ? ` — ${labels(r.iv_ppml_status).label}` : ""}
                      </dd>
                    </div>
                    {r.iv_ppml !== null && r.iv_ppml !== undefined ? (
                      <div>
                        {/* Both standard errors, because neither is the whole
                            answer and the tooltip says what each one leaves
                            out. A resampled range appears only when the run
                            computed one. */}
                        <dt
                          className="text-caption secondary"
                          title={r.iv_ppml_inference || undefined}
                        >
                          Total quantity: how precise
                        </dt>
                        <dd className="m-0">
                          {r.iv_ppml_se_analytic !== null && r.iv_ppml_se_analytic !== undefined
                            ? `plus or minus ${num(r.iv_ppml_se_analytic)} before first-stage noise`
                            : "not available"}
                          {r.iv_ppml_range_low !== null &&
                          r.iv_ppml_range_low !== undefined &&
                          r.iv_ppml_range_high !== null &&
                          r.iv_ppml_range_high !== undefined
                            ? `; resampled range ${num(r.iv_ppml_range_low)} to ${num(
                                r.iv_ppml_range_high,
                              )}`
                            : "; no resampled range in this run"}
                        </dd>
                      </div>
                    ) : null}
                    {r.iv_ppml_sample_losses ? (
                      <div>
                        <dt
                          className="text-caption secondary"
                          title={r.iv_ppml_spj_detail || undefined}
                        >
                          Total quantity: rows the fit could not use
                        </dt>
                        <dd className="m-0">{r.iv_ppml_sample_losses}</dd>
                      </div>
                    ) : null}
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
                      <dt
                        className="text-caption secondary"
                        title={r.price_source_mix || undefined}
                      >
                        Imputed zero-row prices
                      </dt>
                      <dd className="m-0">
                        {r.imputed_price_share !== null && r.imputed_price_share !== undefined
                          ? pct(r.imputed_price_share)
                          : "—"}
                      </dd>
                    </div>
                    <div>
                      {/* How far cost and tariff movements actually moved
                          price here. A proposed change beyond it, or in a gap
                          inside it, gets no volume response. */}
                      <dt className="text-caption secondary">Price movement measured</dt>
                      <dd className="m-0">
                        <span title={r.support_detail || undefined}>
                          {r.support_log_lo !== null &&
                          r.support_log_lo !== undefined &&
                          r.support_log_hi !== null &&
                          r.support_log_hi !== undefined
                            ? `${(100 * (Math.exp(r.support_log_lo) - 1)).toFixed(1)}% to +${(
                                100 *
                                (Math.exp(r.support_log_hi) - 1)
                              ).toFixed(1)}%`
                            : "\u2014"}
                        </span>
                        {r.support_n !== null && r.support_n !== undefined
                          ? `, over ${count(r.support_n)} rows`
                          : ""}
                        {r.support_n_regions !== null &&
                        r.support_n_regions !== undefined &&
                        r.support_n_regions !== 1
                          ? r.support_n_regions === 0
                            ? ", none of it dense enough to use"
                            : ` in ${count(r.support_n_regions)} separate stretches`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-caption secondary">Price movement observed</dt>
                      <dd className="m-0">
                        {r.observed_log_lo !== null &&
                        r.observed_log_lo !== undefined &&
                        r.observed_log_hi !== null &&
                        r.observed_log_hi !== undefined
                          ? `${(100 * (Math.exp(r.observed_log_lo) - 1)).toFixed(1)}% to +${(
                              100 *
                              (Math.exp(r.observed_log_hi) - 1)
                            ).toFixed(1)}%`
                          : "\u2014"}
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
