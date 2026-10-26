/** Action center (Run 5, Phase 42): the queue of generated actions with the
 * shared filter system, an open-actions summary, and the results view
 * (realized vs predicted outcomes and the calibration table). The static demo
 * is read-only with a simulated history, labeled; in API mode the toolbar
 * carries the working identity (configured user name - local identity, not
 * authentication), manual proposals and the ERP price export. */

import * as Tabs from "@radix-ui/react-tabs";
import { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { QuerySection } from "../components/Async";
import { EntityLabel } from "../components/EntityLabel";
import { EntityCombobox } from "../components/filters/Combobox";
import { FilterBar } from "../components/filters/FilterBar";
import { MultiSelect } from "../components/filters/MultiSelect";
import { RangeFilter } from "../components/filters/RangeFilter";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { SortableTable } from "../components/SortableTable";
import { StatusChip } from "../components/StatusChip";
import type { ActionsSection } from "../types/bundle";
import type { ActionRow } from "../types/bundle";
import { ChipDescriptor, rangeChipLabel, useFilterParams } from "../lib/filters";
import { dollars, dollarsShort, count, num, pct } from "../lib/format";
import { useLabels } from "../lib/labels";
import {
  useActionOutcomes,
  useActionUsers,
  useCurrentUser,
  usePageData,
  useTableQuery,
} from "../lib/queries";
import { dataClient } from "../lib/api/client";
import { queryClient } from "../lib/queries";
import { useCompact } from "../lib/useCompact";

const FILTER_KEYS = ["types", "statuses", "owner", "priorities", "evidence", "compliance", "impact"];

const tabClass =
  "rounded-badge px-3 py-1 text-sm data-[state=active]:bg-petrol data-[state=active]:text-paper text-ink/70 hover:text-ink";
const btnClass =
  "rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/[0.87] hover:text-ink disabled:opacity-50";
const inputClass = "rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/[0.87]";

function UserPicker() {
  const users = useActionUsers();
  const [user, setUser] = useCurrentUser();
  if (dataClient.actions.readOnly) return null;
  return (
    <label className="flex items-center gap-2 text-caption">
      <span>
        Working as{" "}
        <span className="secondary">(configured identity, not a login)</span>
      </span>
      <select
        aria-label="Working as"
        className={inputClass}
        value={user ?? ""}
        onChange={(e) => setUser(e.target.value || null)}
      >
        <option value="">Choose a user</option>
        {(users.data ?? []).map((u) => (
          <option key={u.name} value={u.name}>
            {u.name} ({u.roles.join(" and ")})
          </option>
        ))}
      </select>
    </label>
  );
}

function ProposePanel() {
  const [user] = useCurrentUser();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [rationale, setRationale] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (dataClient.actions.readOnly) return null;
  const submit = async () => {
    if (!user) {
      setError("Choose a working identity first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await dataClient.actions.propose(user, {
        action_type: "service_policy",
        title,
        rationale,
      });
      setOpen(false);
      setTitle("");
      setRationale("");
      await queryClient.invalidateQueries({ queryKey: ["table", "actions"] });
      await queryClient.invalidateQueries({ queryKey: ["page", "actions"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "proposal failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <button type="button" className={btnClass} onClick={() => setOpen(!open)}>
        {open ? "Close proposal form" : "Propose an action"}
      </button>
      {open && (
        <div className="panel mt-2 flex flex-col gap-2 px-4 py-3" data-testid="propose-form">
          <label className="text-caption">
            Title
            <input
              className={`${inputClass} mt-1 w-full`}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label className="text-caption">
            Rationale
            <input
              className={`${inputClass} mt-1 w-full`}
              value={rationale}
              onChange={(e) => setRationale(e.target.value)}
            />
          </label>
          <p className="m-0 text-caption secondary">
            Manual proposals start as a service-policy review; price changes are
            proposed from the guidance page so the floor can be checked.
          </p>
          {error && (
            <p className="m-0 text-caption text-amber" role="alert">
              {error}
            </p>
          )}
          <button type="button" className={btnClass} disabled={busy || !title || !rationale} onClick={submit}>
            Submit proposal
          </button>
        </div>
      )}
    </div>
  );
}

function ExportButton() {
  const [user] = useCurrentUser();
  const [summary, setSummary] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (dataClient.actions.readOnly) return null;
  const run = async (dryRun: boolean) => {
    if (!user) {
      setError("Choose a working identity first.");
      return;
    }
    setError(null);
    try {
      const res = await dataClient.actions.priceExport(user, dryRun);
      const s = res.summary as { n_changes?: number };
      setSummary(
        dryRun
          ? `Preview: ${s.n_changes ?? 0} price changes would export.`
          : `Exported ${s.n_changes ?? 0} price changes; ${res.n_transitioned} moved to exported.`,
      );
      if (!dryRun) {
        const blob = new Blob([res.csv], { type: "text/csv" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "prices.csv";
        a.click();
        URL.revokeObjectURL(a.href);
        await queryClient.invalidateQueries({ queryKey: ["table", "actions"] });
        await queryClient.invalidateQueries({ queryKey: ["page", "actions"] });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "export failed");
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <button type="button" className={btnClass} onClick={() => void run(true)} data-testid="export-preview">
        Preview price export
      </button>
      <button type="button" className={btnClass} onClick={() => void run(false)} data-testid="export-run">
        Export approved prices
      </button>
      {summary && (
        <span className="text-caption" data-testid="export-summary">
          {summary}
        </span>
      )}
      {error && (
        <span className="text-caption text-amber" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

/** Linked action title: type + entities when the action targets a pair or a
 * customer, otherwise the generated title. Shared by the desktop table and
 * the compact card list so both read identically. */
function ActionTitle({ a }: { a: ActionRow }) {
  const labels = useLabels();
  return (
    <Link to={`/actions/${a.action_id}`} className="font-medium">
      {a.customer_id ? (
        <>
          {labels(a.action_type).label}:{" "}
          <EntityLabel name={a.customer_name} id={a.customer_id} />
          {a.item_id && (
            <>
              {" / "}
              <EntityLabel name={a.item_name} id={a.item_id} />
            </>
          )}
        </>
      ) : (
        a.title
      )}
    </Link>
  );
}

/** Compact (below 640 px) queue: stacked cards instead of a nine-column
 * table, so the page never overflows sideways on a phone. */
function QueueCards({ items }: { items: ActionRow[] }) {
  const labels = useLabels();
  const [shown, setShown] = useState(15);
  const sorted = useMemo(
    () =>
      [...items].sort(
        (x, y) => (y.expected_impact_cents ?? -Infinity) - (x.expected_impact_cents ?? -Infinity),
      ),
    [items],
  );
  const slice = sorted.slice(0, shown);
  return (
    <>
      <ul className="m-0 list-none p-0" data-testid="queue-cards">
        {slice.map((a) => (
          <li key={a.action_id} className="border-t hairline py-4 first:border-t-0">
            <ActionTitle a={a} />
            {a.blocked && (
              <span className="mt-1 block text-caption text-amber">
                Blocked: {a.blocked_reason}
              </span>
            )}
            <span className="mt-2 flex flex-wrap items-center gap-2">
              <StatusChip status={a.action_type} />
              <StatusChip status={a.status} />
              <StatusChip status={a.evidence_status ?? "insufficient_data"} />
              {a.compliance_flags && <StatusChip status="rp_review_flag" />}
            </span>
            <span className="mt-1.5 block text-caption secondary">
              Expected impact:{" "}
              {a.expected_impact_cents !== null ? dollars(a.expected_impact_cents) : "none stated"}{" "}
              ({labels(a.impact_basis).label.toLowerCase()})
            </span>
            <span className="block text-caption secondary">
              Owner: {a.owner}
              {a.due_date ? `. Due ${a.due_date}` : ""}
              {a.overdue && <span className="font-medium text-garnet"> (overdue)</span>}
            </span>
          </li>
        ))}
      </ul>
      {sorted.length > shown && (
        <button
          type="button"
          className={`${btnClass} mt-2`}
          onClick={() => setShown(shown + 15)}
        >
          Show more ({sorted.length - shown} remaining)
        </button>
      )}
    </>
  );
}

function Queue({ page }: { page: ActionsSection }) {
  const labels = useLabels();
  const compact = useCompact();
  const f = useFilterParams();
  const types = f.getList("types");
  const statuses = f.getList("statuses");
  const priorities = f.getList("priorities");
  const evidence = f.getList("evidence");
  const owner = f.params.get("owner");
  const compliance = f.params.get("compliance") === "1";
  const impactRange = f.getRange("impact"); // dollars

  const table = useTableQuery("actions", {
    page: 1,
    page_size: 5000,
    filters: {
      types: types.length ? types : undefined,
      statuses: statuses.length ? statuses : undefined,
      priorities: priorities.length ? priorities : undefined,
      evidence: evidence.length ? evidence : undefined,
      owner: owner ?? undefined,
      compliance: compliance || undefined,
      impact_min: impactRange.min ?? undefined,
      impact_max: impactRange.max ?? undefined,
    },
  });

  const chips: ChipDescriptor[] = [];
  for (const v of types)
    chips.push({ key: `t-${v}`, label: labels(v).label, onRemove: () => f.setList("types", types.filter((x) => x !== v)) });
  for (const v of statuses)
    chips.push({ key: `s-${v}`, label: labels(v).label, onRemove: () => f.setList("statuses", statuses.filter((x) => x !== v)) });
  for (const v of priorities)
    chips.push({ key: `p-${v}`, label: labels(v).label, onRemove: () => f.setList("priorities", priorities.filter((x) => x !== v)) });
  for (const v of evidence)
    chips.push({ key: `e-${v}`, label: labels(v).label, onRemove: () => f.setList("evidence", evidence.filter((x) => x !== v)) });
  if (owner) chips.push({ key: "owner", label: `Owner: ${owner}`, onRemove: () => f.set("owner", null) });
  if (compliance)
    chips.push({ key: "compliance", label: "Compliance flag", onRemove: () => f.set("compliance", null) });
  const impactChip = rangeChipLabel("Impact", impactRange, " $");
  if (impactChip)
    chips.push({ key: "impact", label: impactChip, onRemove: () => f.setRange("impact", { min: null, max: null }) });

  const columns = useMemo<ColumnDef<ActionRow, unknown>[]>(
    () => [
      {
        header: "Action",
        accessorKey: "title",
        meta: { left: true },
        cell: (c) => (
          <span className="left block min-w-[16rem]">
            <ActionTitle a={c.row.original} />
            {c.row.original.blocked && (
              <span className="mt-1 block text-caption text-amber">
                Blocked: {c.row.original.blocked_reason}
              </span>
            )}
            {c.row.original.compliance_flags && (
              <span className="mt-1 block">
                <StatusChip status="rp_review_flag" />
              </span>
            )}
          </span>
        ),
      },
      {
        header: "Type",
        accessorKey: "action_type",
        cell: (c) => <StatusChip status={c.row.original.action_type} />,
      },
      {
        header: "Status",
        accessorKey: "status",
        cell: (c) => <StatusChip status={c.row.original.status} />,
      },
      {
        header: "Priority",
        accessorKey: "priority",
        cell: (c) => labels(c.row.original.priority).label,
      },
      {
        header: "Evidence",
        accessorKey: "evidence_status",
        cell: (c) => <StatusChip status={c.row.original.evidence_status ?? "insufficient_data"} />,
      },
      {
        header: "Expected impact",
        accessorKey: "expected_impact_cents",
        cell: (c) => (
          <span>
            {c.row.original.expected_impact_cents !== null
              ? dollars(c.row.original.expected_impact_cents)
              : "—"}
            <span className="block text-caption secondary">
              {labels(c.row.original.impact_basis).label}
            </span>
          </span>
        ),
      },
      { header: "Owner", accessorKey: "owner", cell: (c) => c.row.original.owner },
      {
        header: "Due",
        accessorKey: "due_date",
        cell: (c) => (
          <span>
            {c.row.original.due_date ?? "—"}
            {c.row.original.overdue && (
              <span className="block text-caption font-medium text-garnet">Overdue</span>
            )}
          </span>
        ),
      },
    ],
    [labels],
  );

  return (
    <>
      <FilterBar
        chips={chips}
        onClearAll={() => f.clearAll(FILTER_KEYS)}
        shown={table.data?.total}
        total={table.data?.total_unfiltered}
        noun="actions"
      >
        <MultiSelect label="Type" options={page.facets.types ?? []} values={types} onChange={(v) => f.setList("types", v)} renderOption={(v) => labels(v).label} />
        <MultiSelect label="Status" options={page.facets.statuses ?? []} values={statuses} onChange={(v) => f.setList("statuses", v)} renderOption={(v) => labels(v).label} />
        <MultiSelect label="Priority" options={page.facets.priorities ?? []} values={priorities} onChange={(v) => f.setList("priorities", v)} renderOption={(v) => labels(v).label} />
        <MultiSelect label="Evidence" options={page.facets.evidence ?? []} values={evidence} onChange={(v) => f.setList("evidence", v)} renderOption={(v) => labels(v).label} />
        <EntityCombobox
          label="Owner"
          options={(page.facets.owners ?? []).map((o) => ({ id: o, name: null }))}
          value={owner}
          onChange={(v) => f.set("owner", v)}
        />
        <button
          type="button"
          className={`${btnClass} ${compliance ? "bg-petrol text-paper" : ""}`}
          aria-pressed={compliance}
          onClick={() => f.set("compliance", compliance ? null : "1")}
        >
          Compliance flag only
        </button>
        <RangeFilter label="Expected impact" unit="$" range={impactRange} onChange={(r) => f.setRange("impact", r)} />
      </FilterBar>
      <QuerySection query={table} label="action queue" lines={8}>
        {(data) =>
          data.items.length ? (
            compact ? (
              <QueueCards items={data.items} />
            ) : (
              <SortableTable
                data={data.items}
                columns={columns}
                pageSize={15}
                initialSort={[{ id: "expected_impact_cents", desc: true }]}
              />
            )
          ) : (
            <EmptyState title="No actions match these filters">
              Clear a filter or two; the engine generated {count(page.summary.n_actions)} actions
              for this data.
            </EmptyState>
          )
        }
      </QuerySection>
    </>
  );
}

function Results({ page }: { page: ActionsSection }) {
  const labels = useLabels();
  const outcomes = useActionOutcomes();
  return (
    <>
      <Section
        title="Model calibration"
        lede="Prediction error by evidence level, measured against untreated same-segment pairs (a difference-in-differences comparison). Levels are never summed."
      >
        {page.calibration.length === 0 ||
        page.calibration[0].status === "not_enough_implemented_actions" ? (
          <EmptyState title="Not enough implemented actions yet">
            Calibration appears once enough actions are implemented and measured
            {page.calibration.length > 0 && (
              <> ({count(page.calibration[0].n_measured)} measured so far)</>
            )}
            .
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th className="left">Evidence level</th>
                <th>Measured actions</th>
                <th>Median volume error (log points)</th>
                <th>Median price realization</th>
              </tr>
            </thead>
            <tbody>
              {page.calibration.map((c) => (
                <tr key={c.bucket}>
                  <td className="left">{labels(c.bucket).label}</td>
                  <td>{count(c.n_measured)}</td>
                  <td>{num(c.median_abs_volume_error_log_points, 3)}</td>
                  <td>{pct(c.median_realization_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      <Section
        title="Realized vs predicted"
        lede="Every tracked action: how far the invoiced price actually moved, and the volume outcome against the engine's prediction. Insufficient evidence is stated with its reason, never hidden."
      >
        <QuerySection query={outcomes} label="outcomes" lines={6}>
          {(rows) =>
            rows.length ? (
              <div className="overflow-x-auto">
                <table>
                  <thead>
                    <tr>
                      <th className="left">Action</th>
                      <th>Price realization</th>
                      <th>Predicted volume ratio</th>
                      <th>Measured volume ratio</th>
                      <th>Outcome</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.action_id}>
                        <td className="left">
                          <Link to={`/actions/${r.action_id}`}>{r.title}</Link>
                        </td>
                        <td>{pct(r.outcome.realization_pct)}</td>
                        <td>{num(r.outcome.predicted_volume_ratio, 3)}</td>
                        <td>{num(r.outcome.did_volume_ratio, 3)}</td>
                        <td className="left">
                          <StatusChip
                            status={r.outcome.outcome_status}
                            reason={r.outcome.outcome_reason}
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="Nothing tracked yet">
                Outcomes appear after actions are implemented and the measurement
                window elapses.
              </EmptyState>
            )
          }
        </QuerySection>
      </Section>
    </>
  );
}

export function ActionCenterPage() {
  const labels = useLabels();
  const page = usePageData("actions");
  return (
    <div>
      <QuerySection query={page} label="action center" lines={8}>
        {(p) => (
          <>
            <PageHeader
              title="Action center"
              lede={
                p.read_only
                  ? "Findings turned into reviewable actions. This hosted demo is read-only with a simulated review history, labeled demo data; the full propose-review-approve-export workflow runs against the live API."
                  : "Findings turned into reviewable actions: propose, review, approve, export to the ERP, implement, then measure what actually happened."
              }
            />
            <div className="mt-2 grid gap-4 sm:grid-cols-3" data-testid="actions-summary">
              <div className="panel px-5 py-5">
                <h2 className="m-0 text-section">Open actions</h2>
                <p className="m-0 mt-1 text-title font-semibold text-petrol">
                  {count(p.summary.n_actions)}
                </p>
                <p className="m-0 text-caption secondary">
                  {Object.entries(p.summary.by_status)
                    .map(([s, n]) => `${n} ${labels(s).label.toLowerCase()}`)
                    .join(", ")}
                </p>
              </div>
              <div className="panel px-5 py-5">
                <h2 className="m-0 text-section">Expected impact by basis</h2>
                <ul className="m-0 mt-1 list-none p-0 text-caption" data-testid="impact-by-basis">
                  {/* A zero line says nothing - only bases with dollars show. */}
                  {Object.entries(p.summary.impact_by_basis_cents)
                    .filter(([, v]) => v !== 0)
                    .map(([k, v]) => (
                      <li key={k} className="flex justify-between gap-3">
                        <span className="secondary">{labels(k).label}</span>
                        <span className="font-medium">{dollarsShort(v)}</span>
                      </li>
                    ))}
                  {Object.values(p.summary.impact_by_basis_cents).every((v) => v === 0) && (
                    <li className="secondary">No expected dollar impact stated yet.</li>
                  )}
                </ul>
                <p className="mb-0 mt-2 text-caption secondary">
                  Different opportunity levels - never summed into one number.
                </p>
              </div>
              <div className="panel px-5 py-5">
                <h2 className="m-0 text-section">Latest results</h2>
                <p className="m-0 mt-1 text-title font-semibold text-petrol">
                  {count(p.summary.n_measured)} measured
                </p>
                <p className="m-0 text-caption secondary">
                  {p.summary.median_realization_pct !== null &&
                  p.summary.median_realization_pct !== undefined
                    ? `Median price realization ${pct(p.summary.median_realization_pct)}`
                    : "No measured outcomes yet"}
                </p>
                {p.summary.n_compliance_flagged > 0 && (
                  <p className="mb-0 mt-2 text-caption secondary">
                    {count(p.summary.n_compliance_flagged)} actions carry a
                    price-discrimination review flag; approval needs a written
                    justification.
                  </p>
                )}
              </div>
            </div>
            {!dataClient.actions.readOnly && (
              <div className="panel mt-4 flex flex-wrap items-center gap-4 px-4 py-3" data-testid="actions-toolbar">
                <UserPicker />
                <ProposePanel />
                <ExportButton />
              </div>
            )}
            <Tabs.Root defaultValue="queue" className="mt-4">
              <Tabs.List aria-label="Action center views" className="row">
                <Tabs.Trigger value="queue" className={tabClass}>
                  Queue
                </Tabs.Trigger>
                <Tabs.Trigger value="results" className={tabClass}>
                  Results
                </Tabs.Trigger>
              </Tabs.List>
              <Tabs.Content value="queue">
                <Section title="Queue" lede="Every generated action with its derived status; click one for the full history and evidence.">
                  <Queue page={p} />
                </Section>
              </Tabs.Content>
              <Tabs.Content value="results">
                <Results page={p} />
              </Tabs.Content>
            </Tabs.Root>
          </>
        )}
      </QuerySection>
    </div>
  );
}
