/** One action (Run 5, Phase 42): plain-English rationale, evidence links,
 * compliance flags with explanation, the full event history, and the outcome
 * once measured. In API mode the lifecycle buttons drive the maker-checker
 * workflow with optimistic updates rolled back on failure; in the static demo
 * everything is read-only with the simulated history labeled demo data. */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { QuerySection } from "../components/Async";
import { EntityLabel } from "../components/EntityLabel";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { StatusChip } from "../components/StatusChip";
import { dataClient } from "../lib/api/client";
import { ApiError, type ActionDetail, type TransitionKind } from "../lib/api/types";
import { dollars, num, pct } from "../lib/format";
import { useLabels } from "../lib/labels";
import { queryClient, useActionDetail, useCurrentUser } from "../lib/queries";

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
          {num(o.did_volume_ratio, 3)} measured
        </p>
        <p className="m-0 text-caption secondary">
          Predicted {num(o.predicted_volume_ratio, 3)}; the measured ratio compares this pair with{" "}
          {o.n_control_pairs} untreated same-segment pairs (difference-in-differences), so the
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

export function ActionDetailPage() {
  const { actionId } = useParams();
  const labels = useLabels();
  const detail = useActionDetail(actionId ?? null);
  return (
    <div>
      <QuerySection query={detail} label="action detail" lines={10}>
        {(d) => (
          <>
            <PageHeader title={d.action.title} lede={d.action.rationale} />
            <p className="m-0 mt-2">
              <Link to="/actions" className="text-caption">
                Back to the Action center
              </Link>
            </p>
            <Section title="Where it stands">
              <div className="flex flex-wrap items-center gap-3">
                <StatusChip status={d.status} />
                <StatusChip status={d.action.action_type} />
                <span className="text-caption secondary">
                  Priority {labels(d.action.priority).label.toLowerCase()} · owner {d.action.owner}
                  {d.action.due_date ? ` · due ${d.action.due_date}` : ""}
                </span>
                {d.action.expected_impact_cents !== null &&
                  d.action.expected_impact_cents !== undefined && (
                    <span className="text-caption">
                      Expected impact {dollars(d.action.expected_impact_cents)} (
                      {labels(d.action.impact_basis).label.toLowerCase()})
                    </span>
                  )}
              </div>
              {d.action.compliance_flags && (
                <p className="mb-0 mt-3 max-w-2xl text-caption text-amber" data-testid="compliance-note">
                  {labels("rp_review_flag").label}: {labels("rp_review_flag").description}
                </p>
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
              <ul className="m-0 list-none p-0 text-sm">
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
                  <li className="mt-1 secondary">
                    Current price {dollars(d.action.current_price_cents)}
                    {d.action.proposed_price_cents !== null &&
                      ` · proposed ${dollars(d.action.proposed_price_cents)}`}
                    {d.action.floor_price_cents !== null &&
                      ` · binding floor ${dollars(d.action.floor_price_cents)}`}
                    {d.action.uom ? ` · per ${d.action.uom}` : ""}
                  </li>
                )}
              </ul>
            </Section>
            <Section
              title="History"
              lede={
                dataClient.actions.readOnly
                  ? "Simulated review history - demo data."
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
