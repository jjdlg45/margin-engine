"""Load-your-own-data endpoints (polish round). API/local mode only — the
public demo is a static site with no API, so none of this exists there.

Flow: upload a CSV/.xlsx export → the Phase 27 profiler suggests a column
mapping (every guess with a confidence and reason) → the person confirms or
changes each guessed value → the confirmed mapping is saved and validated by
the engine's own loader → the pipeline runs in a background thread and the
served bundle switches to the new results. Suggestions never become silent
decisions: the mapping endpoint refuses to save while any guessed value is
unconfirmed, and validation errors surface as the engine's own actionable
plain-English messages.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_SUFFIXES = (".csv", ".xlsx")

# Enumerated mapping semantics the UI can offer as click choices; everything
# else is free text. Keys match the draft's unconfirmed_fields paths.
_CHOICES: dict[str, list[str]] = {
    "input.delimiter": [",", ";", "\t", "|"],
    "input.negative_format": ["minus", "parentheses"],
    "decimal_separator": [".", ","],
    "lines.net_amount.sign_convention": [
        "natural",
        "positive_reduces_revenue",
        "always_positive_with_type",
    ],
    "lines.unit_cost.cost_basis": ["standard", "average", "fifo", "lifo", "landed"],
    "lines.unit_cost.per_uom": ["line_uom", "base_uom"],
}

TXN_TYPES = ["sale", "return", "credit", "free_good"]


def _err(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _uploads_dir(request: Request) -> Path:
    d: Path = request.app.state.settings.uploads_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _upload_file(request: Request, upload_id: str) -> Path | None:
    folder = _uploads_dir(request) / upload_id
    if not folder.is_dir():
        return None
    files = [p for p in folder.iterdir() if p.suffix.lower() in ALLOWED_SUFFIXES]
    return files[0] if files else None


@router.post("/api/load/upload")
async def upload(request: Request, filename: str) -> Any:
    """Raw-body upload (no multipart): the UI sends the file bytes with the
    original filename as a query parameter."""
    name = Path(filename).name
    if not name or Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        return _err(
            422,
            "Upload a .csv or .xlsx invoice-line export; other formats need a separate extraction step first.",
        )
    body = await request.body()
    if not body:
        return _err(422, "The upload was empty. Choose the export file and try again.")
    if len(body) > MAX_UPLOAD_BYTES:
        return _err(
            413,
            "That file is larger than 200 MB. Run the engine locally on files this size (see the guide page).",
        )
    upload_id = hashlib.sha256(body).hexdigest()[:12]
    folder = _uploads_dir(request) / upload_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(body)
    return {"upload_id": upload_id, "filename": name, "n_bytes": len(body)}


def _confirmables(draft_text: str, profile: Any) -> list[dict[str, Any]]:
    """One entry per unconfirmed guessed value: where it lives in the mapping,
    its current guess, what kind of answer it takes, and the profiler's note."""
    parsed = yaml.safe_load(draft_text)
    unconfirmed: list[str] = list(parsed.get("unconfirmed_fields") or [])
    column_names = [c.name for c in profile.columns]
    notes: dict[str, str] = {}
    for col in profile.columns:
        for cand in col.candidates:
            key = f"lines.{cand.canonical}"
            if key not in notes:
                notes[key] = f"Suggested column {col.name} ({cand.confidence:.0%}): {cand.reason}"
    out: list[dict[str, Any]] = []
    for path in unconfirmed:
        node: Any = parsed
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if path.endswith("transaction_type.code_map") and isinstance(node, dict):
            # One decision per source code: what each code MEANS in the data.
            for code, guess in node.items():
                out.append(
                    {
                        "path": f"{path}.{code}",
                        "value_path": f"{path}.{code}",
                        "current": str(guess),
                        "kind": "choice",
                        "choices": TXN_TYPES,
                        "note": f'What does transaction code "{code}" mean in this export?',
                    }
                )
            continue
        # A field entry like lines.customer_id stores its guess under .column.
        value_path = path
        current = node
        kind = "text"
        choices: list[str] = []
        if isinstance(node, dict) and "column" in node:
            value_path = f"{path}.column"
            current = node["column"]
            kind = "column"
            choices = column_names
        elif path in _CHOICES:
            kind = "choice"
            choices = _CHOICES[path]
        out.append(
            {
                "path": path,
                "value_path": value_path,
                "current": "" if current is None else str(current),
                "kind": kind,
                "choices": choices,
                "note": notes.get(path, ""),
            }
        )
        if isinstance(node, dict) and "format" in node:
            # A detected date format is a guess like any other.
            col = next((c for c in profile.columns if c.name == node.get("column")), None)
            out.append(
                {
                    "path": f"{path}.format",
                    "value_path": f"{path}.format",
                    "current": str(node["format"]),
                    "kind": "choice" if col and col.date_formats else "text",
                    "choices": list(col.date_formats) if col else [],
                    "note": "The date format the samples parse with; ambiguous samples list every parse.",
                }
            )
    # No cost candidate means the person must SAY the source has no cost (the
    # engine refuses to assume; cost-dependent metrics then report
    # cost_unavailable instead of invented numbers).
    if "unit_cost" not in (parsed.get("lines") or {}):
        out.append(
            {
                "path": "cost_unavailable",
                "value_path": "cost_unavailable",
                "current": "true",
                "kind": "choice",
                "choices": ["true"],
                "note": (
                    "No cost column was detected. Confirm the export genuinely has "
                    "no cost - margin, floors and contribution will say cost "
                    "unavailable instead of inventing numbers. If a cost column "
                    "exists, map it with its cost basis in a hand-written mapping."
                ),
            }
        )
    # Every remaining mapped column is confirmable too (e.g. net_amount or the
    # transaction-code column), so a wrong or missing guess is fixable by a
    # click rather than hand-editing YAML.
    emitted = {c["value_path"] for c in out}
    for name, node in (parsed.get("lines") or {}).items():
        if isinstance(node, dict) and "column" in node:
            vp = f"lines.{name}.column"
            if vp not in emitted:
                out.append(
                    {
                        "path": f"lines.{name}",
                        "value_path": vp,
                        "current": str(node["column"]),
                        "kind": "column",
                        "choices": column_names,
                        "note": notes.get(f"lines.{name}", ""),
                    }
                )
    return out


def _profile_with_txn(path: Path, txn_column: str | None) -> Any:
    """Profile the file; when the person repoints the transaction-code column
    to another candidate, the draft (and its code map) follows that choice."""
    from margin_engine.ingest.profiler import profile_file

    prof = profile_file(path)
    if txn_column and txn_column in prof.txn_type_candidates:
        chosen = {txn_column: prof.txn_type_candidates[txn_column]}
        for k, v in prof.txn_type_candidates.items():
            if k != txn_column:
                chosen[k] = v
        prof.txn_type_candidates = chosen
    return prof


@router.post("/api/load/profile/{upload_id}")
def profile_upload(upload_id: str, request: Request, txn_column: str | None = None) -> Any:
    from margin_engine.ingest.profiler import draft_mapping

    path = _upload_file(request, upload_id)
    if path is None:
        return _err(404, f"No upload with id {upload_id}; upload the file first.")
    try:
        prof = _profile_with_txn(path, txn_column)
    except Exception as exc:
        return _err(422, f"The file could not be profiled: {exc}")
    draft = draft_mapping(prof)
    (path.parent / "draft.yaml").write_text(draft, encoding="utf-8", newline="\n")
    return {
        "upload_id": upload_id,
        "filename": path.name,
        "n_rows": prof.n_rows,
        "n_blank_rows": prof.n_blank_rows,
        "encoding": prof.encoding,
        "delimiter": prof.delimiter,
        "decimal_separator": prof.decimal_separator,
        "columns": [
            {
                "name": c.name,
                "dtype_guess": c.dtype_guess,
                "null_rate": c.null_rate,
                "n_distinct": c.n_distinct,
                "date_formats": list(c.date_formats),
                "candidates": [
                    {"canonical": x.canonical, "confidence": x.confidence, "reason": x.reason}
                    for x in c.candidates
                ],
            }
            for c in prof.columns
        ],
        "txn_candidates": list(prof.txn_type_candidates),
        "confirmables": _confirmables(draft, prof),
    }


def _set_path(doc: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node: dict[str, Any] = doc
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


@router.post("/api/load/mapping/{upload_id}")
def save_mapping(upload_id: str, body: dict[str, Any], request: Request) -> Any:
    """Apply the person's confirmations to the draft and save the runnable
    mapping. Every guessed value must be confirmed (its value sent back,
    changed or not) — missing ones are refused by name."""
    from margin_engine.ingest.mapping import load_mapping
    from margin_engine.ingest.profiler import draft_mapping

    path = _upload_file(request, upload_id)
    if path is None:
        return _err(404, f"No upload with id {upload_id}; upload the file first.")
    values = body.get("values")
    if not isinstance(values, dict):
        return _err(422, "Send values: an object of mapping path to confirmed value.")
    txn_column = body.get("txn_column")
    prof = _profile_with_txn(path, str(txn_column) if txn_column else None)
    draft = draft_mapping(prof)
    parsed = yaml.safe_load(draft)
    confirmables = _confirmables(draft, prof)
    known = {c["value_path"] for c in confirmables}
    missing = [c["path"] for c in confirmables if c["value_path"] not in values]
    if missing:
        return _err(
            422,
            "Every suggested value needs your confirmation before the engine will run. "
            f"Still unconfirmed: {', '.join(missing)}.",
        )
    unknown = [str(k) for k in values if str(k) not in known]
    if unknown:
        return _err(
            422,
            "These are not confirmable mapping values for this file: "
            f"{', '.join(sorted(unknown))}. Only the profiler's suggested values can be "
            "confirmed or changed here; anything else belongs in a hand-written mapping.",
        )
    for value_path, value in values.items():
        text = str(value)
        # Booleans arrive as text from the click UI; the schema wants bool.
        typed: Any = text.lower() == "true" if text.lower() in ("true", "false") else text
        _set_path(parsed, str(value_path), typed)
    # All guesses are now human-confirmed; the gate list empties.
    parsed["unconfirmed_fields"] = []
    mapping_path = path.parent / "mapping.yaml"
    mapping_path.write_text(
        yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n"
    )
    try:
        load_mapping(mapping_path)
    except Exception as exc:
        return _err(422, str(exc))
    return {"upload_id": upload_id, "ok": True}


def _jobs(request: Request) -> dict[str, dict[str, str]]:
    app = request.app
    if not hasattr(app.state, "load_jobs"):
        app.state.load_jobs = {}
        app.state.load_jobs_lock = threading.Lock()
    jobs: dict[str, dict[str, str]] = app.state.load_jobs
    return jobs


@router.post("/api/load/run/{upload_id}")
def run_upload(upload_id: str, request: Request) -> Any:
    """Run the full pipeline on the confirmed upload in a background thread
    and switch the served bundle to the results when it finishes."""
    path = _upload_file(request, upload_id)
    if path is None:
        return _err(404, f"No upload with id {upload_id}; upload the file first.")
    mapping_path = path.parent / "mapping.yaml"
    if not mapping_path.exists():
        return _err(409, "Confirm the mapping first; the engine refuses to run on guesses.")
    jobs = _jobs(request)
    lock: threading.Lock = request.app.state.load_jobs_lock
    with lock:
        if any(j.get("state") == "running" for j in jobs.values()):
            return _err(409, "Another run is already in progress; wait for it to finish.")
        jobs[upload_id] = {"state": "running", "message": "Running the pipeline"}
    settings = request.app.state.settings
    store = request.app.state.store
    action_store = request.app.state.action_store

    def work() -> None:
        import polars as pl

        from margin_engine.config import load_rules
        from margin_engine.report.full_run import run_full
        from margin_engine.webexport.build import build_bundle, write_bundle

        try:
            out_dir = path.parent / "engine_out"
            result = run_full(path.parent, mapping_path, settings.rules_path, out_dir)
            jobs[upload_id] = {"state": "running", "message": "Building the web bundle"}
            repo = Path(__file__).resolve().parents[1]
            bundle = build_bundle(
                result,
                load_rules(settings.rules_path),
                repo,
                data_label=f"Your data: {path.name} (local run)",
            )
            bundle_dir = path.parent / "bundle"
            write_bundle(bundle, bundle_dir)
            if result.actions is not None and result.action_events is not None:
                result.actions.write_parquet(bundle_dir / "actions.parquet")
                result.action_events.write_parquet(bundle_dir / "action_events.parquet")
                added = action_store.merge_new(
                    pl.read_parquet(bundle_dir / "actions.parquet"),
                    pl.read_parquet(bundle_dir / "action_events.parquet"),
                )
            else:
                added = 0
            version = store.load_bundle(bundle_dir)
            jobs[upload_id] = {
                "state": "done",
                "message": (
                    f"Done. The site now serves your data (bundle {version}); "
                    f"{added} new action(s) merged into the review queue."
                ),
            }
        except Exception as exc:
            jobs[upload_id] = {"state": "failed", "message": str(exc)}

    threading.Thread(target=work, name=f"load-run-{upload_id}", daemon=True).start()
    return {"upload_id": upload_id, "state": "running"}


@router.get("/api/load/status/{upload_id}")
def run_status(upload_id: str, request: Request) -> Any:
    jobs = _jobs(request)
    return jobs.get(upload_id, {"state": "idle", "message": ""})
