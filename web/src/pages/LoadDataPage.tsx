/** Load your data (polish round). API/local mode only - the public demo is a
 * static site with no API and never shows this page. Upload a CSV or .xlsx
 * export, review the profiler's suggested mapping (every guess shown with its
 * confidence and reason), confirm or change each value with clicks, save the
 * mapping (the engine validates it and refuses guesses), run the pipeline,
 * and open the results. Validation errors are the engine's own plain-English
 * messages, shown verbatim. */

import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { EntityCombobox } from "../components/filters/Combobox";
import { EmptyState, PageHeader, Section } from "../components/Page";
import { queryClient } from "../lib/queries";

const BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "/api";

interface Confirmable {
  path: string;
  value_path: string;
  current: string;
  kind: "column" | "choice" | "text";
  choices: string[];
  note: string;
}

interface ProfileResponse {
  upload_id: string;
  filename: string;
  n_rows: number;
  n_blank_rows: number;
  encoding: string;
  delimiter: string;
  columns: { name: string; dtype_guess: string; null_rate: number; date_formats: string[] }[];
  txn_candidates: string[];
  confirmables: Confirmable[];
}

/** Plain-English label for a mapping path - raw dotted paths never render. */
export function labelFor(c: Confirmable): string {
  const codeMatch = c.path.match(/transaction_type\.code_map\.(.+)$/);
  if (codeMatch) return `Transaction code "${codeMatch[1]}" means`;
  const SPECIAL: Record<string, string> = {
    "input.delimiter": "Field separator",
    "input.encoding": "File encoding",
    "input.negative_format": "Negative amounts written as",
    decimal_separator: "Decimal separator",
    cost_unavailable: "This export has no cost",
  };
  if (SPECIAL[c.path]) return SPECIAL[c.path];
  const tail = c.path.replace(/^lines\./, "");
  const words = tail.replace(/\./g, " ").replace(/_/g, " ");
  const sentence = words.charAt(0).toUpperCase() + words.slice(1);
  return c.value_path.endsWith(".column") && !tail.includes(".") ? `${sentence} column` : sentence;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = (await res.json()) as T & { detail?: string };
  if (!res.ok) throw new Error(data.detail ?? `the request failed (${res.status})`);
  return data;
}

function ChoiceButtons({
  value,
  choices,
  onChange,
}: {
  value: string;
  choices: string[];
  onChange: (v: string) => void;
}) {
  return (
    <span className="flex flex-wrap gap-1" role="group">
      {choices.map((c) => (
        <button
          key={c}
          type="button"
          aria-pressed={value === c}
          onClick={() => onChange(c)}
          className={
            value === c
              ? "rounded-badge border border-petrol bg-petrol px-2 py-1 text-sm text-paper"
              : "rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/70 hover:text-ink"
          }
        >
          {c === "\t" ? "tab" : c}
        </button>
      ))}
    </span>
  );
}

export function LoadDataPage() {
  const navigate = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profile, setProfile] = useState<ProfileResponse | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [txnColumn, setTxnColumn] = useState<string | null>(null);
  const [mappingSaved, setMappingSaved] = useState(false);
  const [runState, setRunState] = useState<{ state: string; message: string } | null>(null);

  const reProfile = async (uploadId: string, txn: string | null) => {
    const q = txn ? `?txn_column=${encodeURIComponent(txn)}` : "";
    const p = await postJson<ProfileResponse>(`/load/profile/${uploadId}${q}`);
    setProfile(p);
    setValues(Object.fromEntries(p.confirmables.map((c) => [c.value_path, c.current])));
    setMappingSaved(false);
  };

  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    setProfile(null);
    setRunState(null);
    try {
      const res = await fetch(`${BASE}/load/upload?filename=${encodeURIComponent(file.name)}`, {
        method: "POST",
        body: file,
      });
      const data = (await res.json()) as { upload_id: string; detail?: string };
      if (!res.ok) throw new Error(data.detail ?? `the upload failed (${res.status})`);
      await reProfile(data.upload_id, null);
      setTxnColumn(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "the upload failed");
    } finally {
      setBusy(false);
    }
  };

  const saveMapping = async () => {
    if (!profile) return;
    setBusy(true);
    setError(null);
    try {
      await postJson(`/load/mapping/${profile.upload_id}`, {
        values,
        txn_column: txnColumn ?? undefined,
      });
      setMappingSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "the mapping could not be saved");
    } finally {
      setBusy(false);
    }
  };

  const run = async () => {
    if (!profile) return;
    setBusy(true);
    setError(null);
    try {
      await postJson(`/load/run/${profile.upload_id}`);
      setRunState({ state: "running", message: "Running the pipeline" });
      const poll = async (): Promise<void> => {
        const res = await fetch(`${BASE}/load/status/${profile.upload_id}`);
        const s = (await res.json()) as { state: string; message: string };
        setRunState(s);
        if (s.state === "running") setTimeout(() => void poll(), 2000);
      };
      setTimeout(() => void poll(), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "the run could not start");
    } finally {
      setBusy(false);
    }
  };

  const openResults = async () => {
    await queryClient.invalidateQueries();
    navigate("/");
  };

  const setValue = (vp: string, v: string) => setValues((prev) => ({ ...prev, [vp]: v }));

  return (
    <div>
      <PageHeader
        title="Load your data"
        lede="Upload a line-level invoice export (CSV or .xlsx). The profiler suggests what each column means; you confirm or change every suggestion before the engine will run - suggestions never become silent decisions. Everything runs on this machine."
      />

      <Section title="1. Upload the export">
        <div className="flex flex-wrap items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".csv,.xlsx"
            aria-label="Choose the export file"
            data-testid="load-file"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void upload(f);
            }}
          />
          {busy && <span className="text-caption secondary">Working…</span>}
        </div>
      </Section>

      {error && (
        <div role="alert" className="mb-4 rounded-[8px] border border-garnet/40 bg-garnet/5 px-4 py-3" data-testid="load-error">
          <p className="m-0 max-w-2xl whitespace-pre-wrap text-sm">{error}</p>
        </div>
      )}

      {profile && (
        <Section
          title="2. Confirm the mapping"
          lede={`${profile.filename}: ${profile.n_rows.toLocaleString("en-US")} rows, ${profile.columns.length} columns, ${profile.encoding} encoding. Each suggestion below shows the profiler's reason; confirm it as-is or pick another value.`}
        >
          {profile.txn_candidates.length > 1 && (
            <div className="mb-5 max-w-2xl">
              <p className="mb-1 mt-0 text-caption secondary">
                Column holding the transaction code (changing it re-reads the code list):
              </p>
              <ChoiceButtons
                value={txnColumn ?? profile.txn_candidates[0]}
                choices={profile.txn_candidates}
                onChange={(v) => {
                  setTxnColumn(v);
                  void reProfile(profile.upload_id, v);
                }}
              />
            </div>
          )}
          <ul className="m-0 list-none p-0" data-testid="confirmables">
            {profile.confirmables.map((c) => (
              <li
                key={c.value_path}
                className="flex flex-wrap items-center gap-x-6 gap-y-2 border-t hairline py-3"
              >
                <span className="w-64 shrink-0">
                  <span className="block font-medium">{labelFor(c)}</span>
                  {c.note && <span className="block text-caption secondary">{c.note}</span>}
                </span>
                {c.kind === "column" && (
                  <EntityCombobox
                    label={`Column for ${labelFor(c).toLowerCase()}`}
                    options={profile.columns.map((col) => ({
                      id: col.name,
                      name: null,
                      hint: col.dtype_guess,
                    }))}
                    value={values[c.value_path] ?? null}
                    onChange={(v) => setValue(c.value_path, v ?? "")}
                  />
                )}
                {c.kind === "choice" && (
                  <ChoiceButtons
                    value={values[c.value_path] ?? c.current}
                    choices={c.choices}
                    onChange={(v) => setValue(c.value_path, v)}
                  />
                )}
                {c.kind === "text" && (
                  <input
                    aria-label={labelFor(c)}
                    className="rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/[0.87]"
                    value={values[c.value_path] ?? ""}
                    onChange={(e) => setValue(c.value_path, e.target.value)}
                  />
                )}
              </li>
            ))}
          </ul>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className="rounded-badge border border-petrol bg-petrol px-3 py-1.5 text-sm text-paper disabled:opacity-50"
              disabled={busy}
              onClick={() => void saveMapping()}
              data-testid="save-mapping"
            >
              Save the mapping
            </button>
            {mappingSaved && (
              <span className="text-caption" data-testid="mapping-saved">
                Mapping saved and validated by the engine.
              </span>
            )}
          </div>
        </Section>
      )}

      {mappingSaved && profile && (
        <Section
          title="3. Run the pipeline"
          lede="The engine reconciles every dollar, classifies every line, and rebuilds every page of this site from your data."
        >
          {!runState && (
            <button
              type="button"
              className="rounded-badge border border-petrol bg-petrol px-3 py-1.5 text-sm text-paper disabled:opacity-50"
              disabled={busy}
              onClick={() => void run()}
              data-testid="run-pipeline"
            >
              Run the pipeline
            </button>
          )}
          {runState && runState.state === "running" && (
            <p className="m-0 text-sm" role="status" data-testid="run-status">
              {runState.message}… this can take a minute on larger files.
            </p>
          )}
          {runState && runState.state === "failed" && (
            <EmptyState title="The run stopped with a validation error">
              <span className="whitespace-pre-wrap" data-testid="run-failed">
                {runState.message}
              </span>
            </EmptyState>
          )}
          {runState && runState.state === "done" && (
            <div className="flex flex-wrap items-center gap-3">
              <p className="m-0 text-sm" data-testid="run-done">
                {runState.message}
              </p>
              <button
                type="button"
                className="rounded-badge border border-petrol bg-petrol px-3 py-1.5 text-sm text-paper"
                onClick={() => void openResults()}
                data-testid="open-results"
              >
                Open the results
              </button>
            </div>
          )}
        </Section>
      )}
    </div>
  );
}
