/** Connections (Run 7, Phase 94).
 *
 * Static demo: an explanatory page from the bundle - what each connector
 * does and how to set it up, with no live controls and no credentials.
 * API/local mode: live status per connector (configured, last sync time and
 * record counts, warnings in plain English), Check connection, Sync now,
 * and schedule settings. Admin-only in production mode (the API enforces
 * it; this page shows the refusal verbatim). */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { QuerySection } from "../components/Async";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { dataSource } from "../lib/api/client";
import { ApiError, type ConnectorCard } from "../lib/api/types";
import { queryClient } from "../lib/queries";
import { usePageData } from "../lib/queries";

const BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "/api";

interface SyncSummary {
  sync_id: string;
  synced_utc: string;
  window?: { since: string | null; until: string };
  records: Record<string, { rows: number; new_or_changed: number; deleted: number }>;
  warnings: string[];
  unchanged_rerun: boolean;
}

interface LiveConnector extends Omit<ConnectorCard, "setup"> {
  configured: boolean;
  missing_settings: { env: string; purpose: string }[];
  scheduled: boolean;
  schedule_interval_s: number | null;
  last_sync: SyncSummary | null;
  last_error: string | null;
}

interface ConnectionsList {
  mode: string;
  connectors: LiveConnector[];
}

async function requestJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = (await res.json()) as T & { detail?: string; code?: string };
  // ApiError, not Error: the query client never retries a 4xx, and a refusal
  // (403 "requires the admin role", 409 "a sync is already running") is an
  // answer to show the reader, not a request to repeat.
  if (!res.ok) {
    throw new ApiError(data.detail ?? `the request failed (${res.status})`, res.status, data.code);
  }
  return data;
}

function useConnections() {
  return useQuery<ConnectionsList>({
    queryKey: ["connections"],
    queryFn: () => requestJson<ConnectionsList>("/connections", "GET"),
  });
}

const dt = (iso: string) => new Date(iso).toLocaleString();

function ProducesList({ produces }: { produces: string[] }) {
  if (produces.length === 0) {
    return <p className="text-sm text-ink/70">Provides: configure pick-up rules first.</p>;
  }
  return (
    <p className="text-sm text-ink/70">
      Provides:{" "}
      {produces.map((f, i) => (
        <span key={f}>
          {i > 0 && ", "}
          <code className="text-sm">{f}</code>
        </span>
      ))}
    </p>
  );
}

function CardShell({
  system,
  description,
  liveVerification,
  children,
}: {
  system: string;
  description: string;
  liveVerification: string;
  children?: React.ReactNode;
}) {
  return (
    <article className="rounded-card border hairline bg-card p-4">
      <h3 className="text-base font-semibold">{system}</h3>
      <p className="mt-1 text-sm text-ink/80">{description}</p>
      <p className="mt-2 text-sm text-ink/70">Verification status: {liveVerification}.</p>
      {children}
    </article>
  );
}

/* ----- static demo view ----- */

function StaticConnections() {
  const q = usePageData("connections");
  return (
    <QuerySection query={q} label="connections" lines={8}>
      {(d) => (
        <div className="space-y-4">
          <p className="max-w-prose text-sm text-ink/80">{d.note}</p>
          <div className="grid gap-4 md:grid-cols-2">
            {d.connectors.map((c) => (
              <CardShell
                key={c.name}
                system={c.system}
                description={c.description}
                liveVerification={c.live_verification}
              >
                <ProducesList produces={c.produces} />
                <p className="mt-2 text-sm text-ink/80">
                  <span className="font-medium">How to set it up:</span> {c.setup}
                </p>
              </CardShell>
            ))}
          </div>
        </div>
      )}
    </QuerySection>
  );
}

/* ----- live (API/local) view ----- */

function LiveCard({ c }: { c: LiveConnector }) {
  const [checkResult, setCheckResult] = useState<string | null>(null);
  const [intervalMinutes, setIntervalMinutes] = useState<string>(
    c.schedule_interval_s != null ? String(Math.round(c.schedule_interval_s / 60)) : "60",
  );
  const check = useMutation({
    mutationFn: () =>
      requestJson<{ ok: boolean; message: string }>(`/connections/${c.name}/check`, "POST"),
    onSuccess: (r) => setCheckResult(`${r.ok ? "Ready" : "Not ready"}: ${r.message}`),
    onError: (e: Error) => setCheckResult(e.message),
  });
  const sync = useMutation({
    mutationFn: () => requestJson<{ sync: SyncSummary }>(`/connections/${c.name}/sync`, "POST"),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["connections"] }),
  });
  const schedule = useMutation({
    mutationFn: () =>
      requestJson<{ interval_s: number; scheduled: boolean; note: string }>(
        `/connections/${c.name}/schedule`,
        "PUT",
        { interval_s: Number(intervalMinutes) * 60 },
      ),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["connections"] }),
  });

  return (
    <CardShell
      system={c.system}
      description={c.description}
      liveVerification={c.live_verification}
    >
      <ProducesList produces={c.produces} />
      {c.configured ? (
        <p className="mt-2 text-sm text-verdigris">Settings are in place.</p>
      ) : (
        <div className="mt-2 text-sm text-garnet">
          <p>Missing settings (environment or .env):</p>
          <ul className="ml-4 list-disc">
            {c.missing_settings.map((m) => (
              <li key={m.env}>
                <code className="text-sm">{m.env}</code> - {m.purpose}
              </li>
            ))}
          </ul>
        </div>
      )}
      {c.last_sync ? (
        <div className="mt-2 text-sm text-ink/80">
          <p>
            Last sync {dt(c.last_sync.synced_utc)}
            {c.last_sync.unchanged_rerun ? " (no changes since the one before)" : ""}:
          </p>
          <ul className="ml-4 list-disc">
            {Object.entries(c.last_sync.records).map(([table, r]) => (
              <li key={table}>
                <code className="text-sm">{table}</code>: {r.rows.toLocaleString()} rows (
                {r.new_or_changed.toLocaleString()} new or changed
                {r.deleted > 0 ? `, ${r.deleted.toLocaleString()} deleted` : ""})
              </li>
            ))}
          </ul>
          {c.last_sync.warnings.map((w) => (
            <p key={w} className="mt-1 text-amber">
              Note: {w}
            </p>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-sm text-ink/70">No sync has run yet.</p>
      )}
      {c.last_error && <p className="mt-2 text-sm text-garnet">Last failure: {c.last_error}</p>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="rounded-badge border hairline bg-card px-3 py-1 text-sm hover:text-ink"
          disabled={check.isPending}
          onClick={() => check.mutate()}
        >
          {check.isPending ? "Checking…" : "Check connection"}
        </button>
        <button
          type="button"
          className="rounded-badge border border-petrol bg-petrol px-3 py-1 text-sm text-paper disabled:opacity-60"
          disabled={sync.isPending}
          onClick={() => sync.mutate()}
        >
          {sync.isPending ? "Syncing…" : "Sync now"}
        </button>
        <label className="flex items-center gap-1 text-sm text-ink/80">
          Sync every
          <input
            type="number"
            min={1}
            value={intervalMinutes}
            onChange={(e) => setIntervalMinutes(e.target.value)}
            className="w-16 rounded-badge border hairline bg-card px-2 py-1 text-sm"
            aria-label={`Sync interval in minutes for ${c.system}`}
          />
          minutes
        </label>
        <button
          type="button"
          className="rounded-badge border hairline bg-card px-3 py-1 text-sm hover:text-ink"
          disabled={schedule.isPending || !Number.isFinite(Number(intervalMinutes))}
          onClick={() => schedule.mutate()}
        >
          Save schedule
        </button>
      </div>
      <div role="status" className="mt-2 space-y-1 text-sm">
        {checkResult && <p>{checkResult}</p>}
        {sync.isError && <p className="text-garnet">{(sync.error as Error).message}</p>}
        {sync.isSuccess && !sync.isPending && (
          <p className="text-verdigris">
            Sync finished
            {sync.data.sync.unchanged_rerun ? " - no changes since the last one." : "."}
          </p>
        )}
        {schedule.isError && <p className="text-garnet">{(schedule.error as Error).message}</p>}
        {schedule.isSuccess && (
          <p className="text-verdigris">
            Schedule saved: every {Math.round(schedule.data.interval_s / 60)} minutes.
            {schedule.data.note ? ` ${schedule.data.note}` : ""}
          </p>
        )}
      </div>
    </CardShell>
  );
}

function LiveConnections() {
  const q = useConnections();
  return (
    <QuerySection query={q} label="connections" lines={8}>
      {(d) => (
        <div className="space-y-4">
          {d.mode === "local" && (
            <p className="max-w-prose text-sm text-ink/70">
              This server runs in local single-user mode. In production mode these controls
              require the admin role.
            </p>
          )}
          {d.connectors.length === 0 ? (
            <EmptyState title="No connectors available">The engine build has no registered connectors.</EmptyState>
          ) : (
            <div className="grid gap-4 xl:grid-cols-2">
              {d.connectors.map((c) => (
                <LiveCard key={c.name} c={c} />
              ))}
            </div>
          )}
        </div>
      )}
    </QuerySection>
  );
}

export function ConnectionsPage() {
  return (
    <div>
      <PageHeader
        title="Connections"
        lede={
          dataSource === "http"
            ? "Pull data from your systems on a schedule instead of manual exports. Every connector writes the same files the engine already reads, so nothing downstream changes."
            : "How the engine pulls data from company systems on a schedule instead of manual exports. Every connector writes the same files the engine already reads, so nothing downstream changes."
        }
      />
      <Section
        title="System connectors"
        lede="A connector is only described as working once it has run end to end against the real service or its official sandbox; anything verified only against documented schemas says so."
      >
        {dataSource === "http" ? <LiveConnections /> : <StaticConnections />}
      </Section>
    </div>
  );
}
