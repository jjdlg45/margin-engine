/** One action (Run 5, Phase 42): plain-English rationale, evidence links,
 * compliance flags with explanation, the full event history, and the outcome
 * once measured. In API mode the lifecycle buttons drive the maker-checker
 * workflow with optimistic updates rolled back on failure; in the static demo
 * everything is read-only with the simulated history labeled demo data. */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { QuerySection } from "../components/Async";
import { EntityLabel } from "../components/EntityLabel";
import { Breadcrumbs, EmptyState, PageHeader, Section } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import { dataClient, isSandbox } from "../lib/api/client";
import { ApiError, type ActionDetail, type TransitionKind } from "../lib/api/types";
import { SandboxBanner } from "../components/SandboxBanner";
import { StepTracker } from "../components/StepTracker";
import type { ActionRow } from "../types/bundle";
import { dollars, num, pct } from "../lib/format";
import { useLabels } from "../lib/labels";
import {
  queryClient,
  useActionDetail,
  useComplianceReview,
  useCurrentUser,
} from "../lib/queries";

/** The facts beside a Robinson-Patman review flag.
 *
 * The flag on its own is a price difference, and a price difference is the
 * start of the question. The defenses that matter - cost justification,
 * meeting competition, functional differences between the two buyers - live
 * in these numbers, so the reviewer sees them here rather than having to
 * open an export. Cost to serve is supporting evidence, never a conclusion,
 * and the page never says whether anything is lawful.
 */
function ReviewFacts({ action }: { action: ActionRow }) {
  const { data } = useComplianceReview();
  const pairs = (data ?? []).filter(
    (p) =>
      p.item_id === action.item_id &&
      (p.customer_id_a === action.customer_id || p.customer_id_b === action.customer_id),
  );
  if (pairs.length === 0) return null;
  return (
    <div className="mt-3 max-w-2xl text-sm" data-testid="review-facts">
      {pairs.map((p) => {
        const mine = p.customer_id_a === action.customer_id;
        const other = mine ? p.customer_id_b : p.customer_id_a;
        const myNet = mine ? p.recommended_net_a_cents : p.recommended_net_b_cents;
        const theirNet = mine ? p.recommended_net_b_cents : p.recommended_net_a_cents;
        const myCts = mine ? p.cts_per_unit_a_cents : p.cts_per_unit_b_cents;
        const theirCts = mine ? p.cts_per_unit_b_cents : p.cts_per_unit_a_cents;
        const myQty = mine ? p.quantity_a : p.quantity_b;
        const theirQty = mine ? p.quantity_b : p.quantity_a;
        const myChannel = mine ? p.channel_a : p.channel_b;
        const theirChannel = mine ? p.channel_b : p.channel_a;
        return (
          <div key={`${p.customer_id_a}-${p.customer_id_b}-${p.item_id}`} className="mt-2">
            <p className="m-0">
              Compared with {other} ({p.pair_basis}):{" "}
              {p.price_difference_pct !== null ? pct(p.price_difference_pct / 100) : "—"} apart on
              the invoice price,{" "}
              {p.net_difference_pct !== null ? pct(p.net_difference_pct / 100) : "—"} after
              discounts and rebates.
            </p>
            <ul className="m-0 mt-1 list-disc pl-5 text-caption">
              <li>
                Recommended after deductions: {myNet !== null ? dollars(myNet) : "—"} here,{" "}
                {theirNet !== null ? dollars(theirNet) : "—"} there
              </li>
              <li>
                Volume: {myQty !== null ? num(myQty) : "—"} against{" "}
                {theirQty !== null ? num(theirQty) : "—"}; channel {myChannel ?? "not stated"}{" "}
                against {theirChannel ?? "not stated"}
              </li>
              <li>
                Cost to serve: {myCts !== null ? dollars(myCts) : "—"} against{" "}
                {theirCts !== null ? dollars(theirCts) : "—"}
                {p.cts_method ? ` (${p.cts_method})` : ""} - supporting evidence, not a conclusion
              </li>
              <li>Facts available: {p.data_coverage}</li>
              <li>
                Reviewer decision:{" "}
                {p.reviewer_decision ?? "none recorded"}
                {p.meeting_competition_record
                  ? `; meeting-competition record: ${p.meeting_competition_record}`
                  : "; no meeting-competition record"}
              </li>
            </ul>
          </div>
        );
      })}
    </div>
  );
}

const btnClass =
  "rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/[0.87] hover:text-ink disabled:opacity-50";
const inputClass = "rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/[0.87]";

/** Which buttons each current status offers (role checks happen server-side;
 * the API's refusal reason is shown verbatim). */
const NEXT: Record<string, { kind: TransitionKind; label: string; needsNote?: boolean }[]> = {
  proposed: [{ kind: "submit", label: "Submit for review" }],
  under_review: [
    { kind: "approve", label: "Approve", needsNote: true },
    { kind: "reject", label: "Reject", needsNote: true },
  ],
  rejected: [{ kind: "rework", label: "Rework (back to proposed)" }],
  approved: [],
  exported: [{ kind: "implement", label: "Mark implemented" }],
  implemented: [],
  measured: [],
};

function WorkflowButtons({ detail, actionId }: { detail: ActionDetail; actionId: string }) {
  const [user] = useCurrentUser();
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (dataClient.actions.readOnly) return null;
  const options = NEXT[detail.status] ?? [];
  if (detail.action.blocked) {
    return (
      <p className="m-0 text-caption text-amber">
        Blocked: {detail.action.blocked_reason} - no transition is possible.
      </p>
    );
  }
  if (!options.length) {
    return (
      <p className="m-0 text-caption secondary">
        {detail.status === "approved"
          ? "Approved - it leaves this status through the price export."
          : "No manual transition from this status."}
      </p>
    );
  }
  const act = async (kind: TransitionKind) => {
    if (!user) {
      setError("Choose a working identity in the Action center toolbar first.");
      return;
    }
    setBusy(true);
    setError(null);
    const key = ["action-detail", actionId];
    const previous = queryClient.getQueryData<ActionDetail>(key);
    // Optimistic: show the target status immediately; roll back on failure.
    const target: Record<TransitionKind, string> = {
      submit: "under_review",
      approve: "approved",
      reject: "rejected",
      rework: "proposed",
      implement: "implemented",
    };
    if (previous) {
      queryClient.setQueryData<ActionDetail>(key, { ...previous, status: target[kind] });
    }
    try {
      await dataClient.actions.transition(actionId, kind, user, note);
      setNote("");
      await queryClient.invalidateQueries({ queryKey: key });
      await queryClient.invalidateQueries({ queryKey: ["table", "actions"] });
      await queryClient.invalidateQueries({ queryKey: ["page", "actions"] });
    } catch (e) {
      if (previous) queryClient.setQueryData(key, previous); // rollback
      const msg =
        e instanceof ApiError && e.code
          ? `${e.message}`
          : e instanceof Error
            ? e.message
            : "the transition failed";
      setError(msg);
    } finally {
      setBusy(false);
    }
  };
  const needsNote = options.some((o) => o.needsNote);
  return (
    <div className="flex flex-col gap-2" data-testid="workflow">
      {needsNote && (
        <label className="text-caption">
          {detail.action.compliance_flags
            ? "Written justification (required: this action carries a compliance flag)"
            : "Reason (recorded with the decision)"}
          <input
            className={`${inputClass} mt-1 w-full`}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            data-testid="decision-note"
          />
        </label>
      )}
      <div className="flex flex-wrap gap-2">
        {options.map((o) => (
          <button
            key={o.kind}
            type="button"
            className={btnClass}
            disabled={busy}
            onClick={() => void act(o.kind)}
            data-testid={`transition-${o.kind}`}
          >
            {o.label}
          </button>
        ))}
      </div>
      {error && (
        <p className="m-0 text-caption text-amber" role="alert" data-testid="workflow-error">
          {error}
        </p>
      )}
    </div>
  );
}

function OwnerEditor({ detail, actionId }: { detail: ActionDetail; actionId: string }) {
  const [user] = useCurrentUser();
  const [owner, setOwner] = useState(detail.action.owner);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  if (dataClient.actions.readOnly) return null;
  const editable = ["proposed", "under_review", "rejected"].includes(detail.status);
  if (!editable || detail.action.blocked) return null;
  const save = async () => {
    if (!user) {
      setError("Choose a working identity first.");
      return;
    }
    setError(null);
    setSaved(false);
    try {
      await dataClient.actions.edit(actionId, user, { owner });
      setSaved(true);
      await queryClient.invalidateQueries({ queryKey: ["action-detail", actionId] });
      await queryClient.invalidateQueries({ queryKey: ["table", "actions"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "edit failed");
    }
  };
  return (
    <div className="flex flex-wrap items-end gap-2">
      <label className="text-caption">
        Owner
        <input
          className={`${inputClass} mt-1 block`}
          value={owner}
          onChange={(e) => setOwner(e.target.value)}
          data-testid="owner-input"
        />
      </label>
      <button type="button" className={btnClass} onClick={() => void save()} data-testid="owner-save">
        Save owner
      </button>
      {saved && <span className="text-caption secondary">Saved.</span>}
      {error && (
        <span className="text-caption text-amber" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

function OutcomeCard({ detail }: { detail: ActionDetail }) {
  const o = detail.outcome;
  if (!o) {
    return (
      <EmptyState title="Not measured yet">
        Outcomes appear after the action is implemented and the measurement
        window elapses.
      </EmptyState>
    );
  }
  if (o.outcome_status !== "measured") {
    return (
      <EmptyState title="Insufficient evidence">{o.outcome_reason}</EmptyState>
    );
  }
  return (
    <div className="grid gap-4 sm:grid-cols-3" data-testid="outcome">
      <div className="panel px-5 py-4">
        <h3 className="m-0 text-section">Price realization</h3>
        <p className="m-0 mt-1 text-title font-semibold text-petrol">{pct(o.realization_pct)}</p>
        <p className="m-0 text-caption secondary">
          {dollars(o.price_before_cents)} before, {dollars(o.price_after_cents)} after, toward a
          proposed {dollars(o.proposed_price_cents)}.
        </p>
      </div>
      <div className="panel px-5 py-4">
        <h3 className="m-0 text-section">Volume vs prediction</h3>
        <p className="m-0 mt-1 text-title font-semibold text-petrol">
          {num(o.control_adjusted_volume_ratio, 3)} measured
        </p>
        <p className="m-0 text-caption secondary">
          Predicted {num(o.predicted_volume_ratio, 3)}; the measured ratio compares this pair with{" "}
          {o.n_control_pairs} untreated same-segment pairs, so the
          general trend never reads as a price effect.
        </p>
      </div>
      <div className="panel px-5 py-4">
        <h3 className="m-0 text-section">Margin outcome</h3>
        <p className="m-0 mt-1 text-title font-semibold text-petrol">
          {pct(o.pocket_margin_after)}
        </p>
        <p className="m-0 text-caption secondary">
          Pocket margin after, vs {pct(o.pocket_margin_before)} before; contribution{" "}
          {dollars(o.contribution_before_cents)} to {dollars(o.contribution_after_cents)} over the{" "}
          {o.window_days}-day windows.
        </p>
      </div>
    </div>
  );
}

/** Pass-through actions have no customer-item pair, so their evidence is the
 * scenario itself: the grid it came from, its scope, the chosen pass-through
 * level and the incremental cost - all from the engine's exports. */
function PassThroughEvidence({ action }: { action: ActionRow }) {
  if (action.action_type !== "cost_pass_through") return null;
  const sep = action.scope.indexOf(":");
  const scopeType = sep > 0 ? action.scope.slice(0, sep) : null;
  const scopeValue = sep > 0 ? action.scope.slice(sep + 1) : action.scope;
  return (
    <>
      {scopeType && (
        <li>
          Scenario:{" "}
          <Link
            to={`/scenarios?scenario=${encodeURIComponent(`+10% landed cost: ${scopeType} ${scopeValue}`)}`}
          >
            open the pass-through grid
          </Link>{" "}
          (every point precomputed by the engine)
        </li>
      )}
      <li className="mt-1">
        Scope: {scopeType === "supplier" ? "supplier" : "category"} {scopeValue}
      </li>
      {action.pass_through_level !== null && action.pass_through_level !== undefined && (
        <li className="mt-1">
          Pass-through level: {pct(action.pass_through_level)} of the cost increase priced through
        </li>
      )}
      {action.incremental_cost_cents !== null && action.incremental_cost_cents !== undefined && (
        <li className="mt-1">
          Incremental cost: {dollars(action.incremental_cost_cents)} over the baseline window
        </li>
      )}
    </>
  );
}

export function ActionDetailPage() {
  const { actionId } = useParams();
  const labels = useLabels();
  const detail = useActionDetail(actionId ?? null);
  return (
    <div>
      <QuerySection query={detail} label="action detail" lines={10}>
        {(d) => (
          <>
            <Breadcrumbs
              trail={[{ label: "Action center", to: "/actions" }, { label: d.action.title }]}
            />
            <PageHeader title={d.action.title} lede={d.action.rationale} />
            <SandboxBanner />
            <Section title="Where it stands">
              <div className="mb-4">
                <StepTracker status={d.status} />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <StatusChip status={d.status} />
                <StatusChip status={d.action.action_type} />
              </div>
              {/* Labeled fields, never a dot-separated meta string. */}
              <dl className="m-0 mt-4 grid grid-cols-2 gap-x-8 gap-y-3 text-sm sm:grid-cols-4">
                <div>
                  <dt className="text-caption secondary">Priority</dt>
                  <dd className="m-0 font-medium">{labels(d.action.priority).label}</dd>
                </div>
                <div>
                  <dt className="text-caption secondary">Owner</dt>
                  <dd className="m-0 font-medium">{d.action.owner}</dd>
                </div>
                <div>
                  <dt className="text-caption secondary">Due date</dt>
                  <dd className="m-0 font-medium">
                    {d.action.due_date ?? "None set"}
                    {d.action.overdue && (
                      <span className="font-medium text-garnet"> (overdue)</span>
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-caption secondary">Expected impact</dt>
                  <dd className="m-0 font-medium">
                    {d.action.expected_impact_cents !== null &&
                    d.action.expected_impact_cents !== undefined ? (
                      <>
                        {dollars(d.action.expected_impact_cents)}{" "}
                        <span className="font-normal secondary">
                          {labels(d.action.impact_basis).label.toLowerCase()}
                        </span>
                      </>
                    ) : (
                      "None stated"
                    )}
                  </dd>
                </div>
              </dl>
              {d.action.compliance_flags && (
                <>
                  <p
                    className="mb-0 mt-3 max-w-2xl text-caption text-amber"
                    data-testid="compliance-note"
                  >
                    {labels("rp_review_flag").label}: {labels("rp_review_flag").description}
                  </p>
                  <ReviewFacts action={d.action} />
                </>
              )}
              {d.action.blocked && (
                <p className="mb-0 mt-3 max-w-2xl text-caption text-amber">
                  Blocked: {d.action.blocked_reason}
                </p>
              )}
              <div className="mt-4 flex flex-col gap-3">
                <WorkflowButtons detail={d} actionId={actionId!} />
                <OwnerEditor detail={d} actionId={actionId!} />
              </div>
            </Section>
            <Section title="Evidence" lede="Every number traces to engine output.">
              <ul className="m-0 list-none p-0 text-sm" data-testid="evidence-list">
                {d.action.customer_id && d.action.item_id && (
                  <li>
                    Guidance band for{" "}
                    <EntityLabel name={d.action.customer_name} id={d.action.customer_id} /> /{" "}
                    <EntityLabel name={d.action.item_name} id={d.action.item_id} />:{" "}
                    <Link
                      to={`/guidance?customer=${encodeURIComponent(d.action.customer_id)}&item=${encodeURIComponent(d.action.item_id)}`}
                    >
                      open the band chart and explanation
                    </Link>
                  </li>
                )}
                {d.action.segment !== null && d.action.segment !== undefined && (
                  <li className="mt-1">
                    <Link to="/segments">Segment rules</Link> and{" "}
                    <Link to="/sensitivity">price sensitivity evidence</Link>
                    {d.action.evidence_status && (
                      <>
                        {" "}
                        (<StatusChip status={d.action.evidence_status} />)
                      </>
                    )}
                  </li>
                )}
                {d.action.current_price_cents !== null && (
                  <li className="mt-3">
                    <dl className="m-0 grid grid-cols-2 gap-x-8 gap-y-2 sm:grid-cols-4">
                      <div>
                        <dt className="text-caption secondary">Current price</dt>
                        <dd className="m-0 font-medium">
                          {dollars(d.action.current_price_cents)}
                        </dd>
                      </div>
                      {d.action.proposed_price_cents !== null && (
                        <div>
                          <dt className="text-caption secondary">Proposed price</dt>
                          <dd className="m-0 font-medium">
                            {dollars(d.action.proposed_price_cents)}
                          </dd>
                        </div>
                      )}
                      {d.action.floor_price_cents !== null && (
                        <div>
                          <dt className="text-caption secondary">Binding floor</dt>
                          <dd className="m-0 font-medium">{dollars(d.action.floor_price_cents)}</dd>
                        </div>
                      )}
                      {d.action.uom && (
                        <div>
                          <dt className="text-caption secondary">Unit</dt>
                          <dd className="m-0 font-medium">{d.action.uom}</dd>
                        </div>
                      )}
                    </dl>
                  </li>
                )}
                <PassThroughEvidence action={d.action} />
                {d.action.action_type === "service_policy" && d.action.customer_id && (
                  <li className="mt-1">
                    Order and activity economics for{" "}
                    <EntityLabel name={d.action.customer_name} id={d.action.customer_id} />:{" "}
                    <Link to={`/customers?customer=${encodeURIComponent(d.action.customer_id)}`}>
                      open the customer drill-down
                    </Link>{" "}
                    (contribution after activity costs, order sizes, service flags)
                  </li>
                )}
                {d.action.action_type === "rebate_discount_review" && d.action.customer_id && (
                  <li className="mt-1">
                    Leakage for{" "}
                    <EntityLabel name={d.action.customer_name} id={d.action.customer_id} /> against
                    its segment:{" "}
                    <Link to={`/opportunity?customer=${encodeURIComponent(d.action.customer_id)}`}>
                      open the opportunity view
                    </Link>{" "}
                    (observed leakage by component, reconciled dollars)
                  </li>
                )}
                {/* Every action type shows evidence - never an empty heading. */}
                <li className="mt-1 secondary">
                  Generation rules and guardrails for every action type:{" "}
                  <Link to="/methodology">Data & methodology</Link>
                </li>
              </ul>
            </Section>
            <Section
              title="History"
              lede={
                dataClient.actions.readOnly
                  ? "Simulated review history - demo data."
                  : isSandbox
                    ? "Simulated demo history plus your sandbox changes - stored only on this device; status is always derived from the log."
                    : "The append-only event log; status is always derived from it."
              }
            >
              <ol className="m-0 list-none p-0 text-sm" data-testid="event-history">
                {d.events.map((e) => (
                  <li key={e.seq} className="border-b hairline py-2 last:border-b-0">
                    <span className="font-medium">{labels(e.to_status).label}</span>{" "}
                    <span className="secondary">
                      by {e.who} on {e.at.slice(0, 10)}
                    </span>
                    {e.note && <span className="block text-caption">{e.note}</span>}
                  </li>
                ))}
              </ol>
              {d.edits.length > 0 && (
                <details className="mt-2 text-caption">
                  <summary>Edit audit ({d.edits.length})</summary>
                  <ul className="m-0 list-none p-0">
                    {d.edits.map((e, i) => (
                      <li key={i}>
                        {String(e.field)} changed from {String(e.old_value ?? "—")} to{" "}
                        {String(e.new_value ?? "—")} by {String(e.who)}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </Section>
            <Section title="Outcome" lede="Measured against untreated same-segment pairs once the window elapses.">
              <OutcomeCard detail={d} />
            </Section>
          </>
        )}
      </QuerySection>
    </div>
  );
}
