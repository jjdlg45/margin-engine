"""System-connector framework (Run 7, Phase 90).

A system connector pulls records from a company system (ERP, accounting,
CRM, a dropped file) and materializes them as the SAME canonical CSV files
the engine already ingests through a mapping. Nothing downstream changes:
mapping, validation, reconciliation and every test read the materialized
folder exactly as they read a manual export.

Contract every connector honours (tested in tests/test_connector_framework.py):
- incremental sync with a locally stored cursor; re-running is idempotent;
  a full resync is available;
- late-arriving edits are handled by a re-fetch window on the cursor and by
  upsert-by-key into the local state; deletions arrive as tombstones where
  the source API exposes them;
- a partial failure leaves the previous good sync in place: nothing is
  half-written, the cursor does not advance, and the error is plain English;
- every sync writes a manifest (connector name and version, source, time
  window, record counts per object, checksum per file, warnings, cursor
  used) that the run manifest links to;
- no credential value ever appears in any file the framework writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from margin_engine.connectors.base import ConnectorError

FRAMEWORK_VERSION = "1.0"
SYNC_MANIFEST_NAME = "sync_manifest.json"


@dataclass(frozen=True)
class SettingSpec:
    """One setting a connector needs, read from the environment or .env.

    `secret` settings are scrubbed from everything the framework writes.
    `optional` settings have a default or belong to an alternative (e.g.
    watched folder vs SFTP); `check()` reports whether the combination works.
    """

    env: str
    purpose: str
    secret: bool = True
    optional: bool = False


@dataclass(frozen=True)
class ConnectorInfo:
    """What `describe()` returns: enough for the Connections screen and docs."""

    name: str
    version: str
    system: str
    description: str
    produces: tuple[str, ...]
    required_settings: tuple[SettingSpec, ...]
    scopes: tuple[str, ...]
    # Honesty rule (Run 7): "verified live against <service> on <date>" or
    # "built against the documented API, not yet verified live".
    live_verification: str
    mapping_file: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """Credentials-and-reachability check, in plain English."""

    ok: bool
    message: str


@dataclass
class TableBatch:
    """New or changed rows for one canonical table, plus tombstones.

    `deletes` rows may carry the key columns or any column subset the table
    holds (e.g. a parent-document column, so one tombstone removes every
    line of a deleted invoice)."""

    key: tuple[str, ...]
    upserts: pl.DataFrame
    deletes: pl.DataFrame | None = None


@dataclass(frozen=True)
class RawFile:
    """A validated source file placed verbatim into the sync folder.

    The file-drop connector uses this: a dropped export is already in the
    shape its mapping declares, so the framework copies the bytes unchanged
    instead of parsing and re-writing them. The framework keeps the latest
    accepted copy per name, so later syncs without a new drop still
    materialize a complete folder.
    """

    name: str
    source: Path
    rows: int


@dataclass
class SyncBatch:
    """Everything one `sync(since)` call fetched."""

    tables: dict[str, TableBatch]
    cursor: str
    window_start: str | None
    window_end: str
    warnings: list[str] = field(default_factory=list)
    raw_files: list[RawFile] = field(default_factory=list)


class SystemConnector(ABC):
    """The Run 7 connector interface."""

    @abstractmethod
    def describe(self) -> ConnectorInfo: ...

    @abstractmethod
    def check(self) -> CheckResult: ...

    @abstractmethod
    def sync(self, since: str | None) -> SyncBatch:
        """Fetch records new or changed since the cursor (None = everything).

        Connectors apply their own re-fetch window to `since` so that
        late-arriving edits inside the window are picked up again; upserting
        by key makes that idempotent.
        """
        ...


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _secret_values(info: ConnectorInfo) -> list[str]:
    vals = []
    for spec in info.required_settings:
        if spec.secret:
            v = os.environ.get(spec.env)
            if v:
                vals.append(v)
    return vals


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s in text:
            text = text.replace(s, "[redacted]")
    return text


class SyncStore:
    """Local state for one connector: cursor, merged table state, sync folders.

    Layout under `<root>/<connector-name>/`:
      cursor.json              the incremental cursor
      state/<table>.parquet    merged current state per canonical table
      syncs/<sync-id>/         one folder per materialized sync (canonical
                               CSVs + sync_manifest.json)
      latest.json              pointer to the most recent good sync
    """

    def __init__(self, root: Path, connector_name: str) -> None:
        self.dir = root / connector_name
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def syncs_dir(self) -> Path:
        return self.dir / "syncs"

    def cursor(self) -> str | None:
        p = self.dir / "cursor.json"
        if not p.exists():
            return None
        value = json.loads(p.read_text(encoding="utf-8")).get("cursor")
        return str(value) if value is not None else None

    def _write_cursor(self, cursor: str) -> None:
        tmp = self.dir / "cursor.json.tmp"
        tmp.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")
        os.replace(tmp, self.dir / "cursor.json")

    def latest(self) -> dict[str, Any] | None:
        """The most recent good sync's manifest, or None before the first."""
        p = self.dir / "latest.json"
        if not p.exists():
            return None
        pointer = json.loads(p.read_text(encoding="utf-8"))
        manifest = self.syncs_dir / str(pointer["sync_id"]) / SYNC_MANIFEST_NAME
        if not manifest.exists():
            return None
        out: dict[str, Any] = json.loads(manifest.read_text(encoding="utf-8"))
        return out

    def latest_dir(self) -> Path | None:
        """The folder of canonical files the engine should read, or None."""
        info = self.latest()
        if info is None:
            return None
        return self.syncs_dir / str(info["sync_id"])

    def _write_latest(self, sync_id: str) -> None:
        tmp = self.dir / "latest.json.tmp"
        tmp.write_text(json.dumps({"sync_id": sync_id}), encoding="utf-8")
        os.replace(tmp, self.dir / "latest.json")

    def _state_path(self, table: str) -> Path:
        return self.dir / "state" / f"{table}.parquet"

    def read_state(self, table: str) -> pl.DataFrame | None:
        p = self._state_path(table)
        return pl.read_parquet(p) if p.exists() else None

    def read_raw_meta(self) -> dict[str, int]:
        """Row counts of the raw files carried in state (file drop)."""
        p = self.dir / "state_files" / "meta.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()}

    def clear_state(self) -> None:
        state = self.dir / "state"
        if state.exists():
            shutil.rmtree(state)


def _merge_table(existing: pl.DataFrame | None, batch: TableBatch, table: str) -> pl.DataFrame:
    """Upsert-by-key into the current state, then apply tombstones."""
    key = list(batch.key)
    for col in key:
        if col not in batch.upserts.columns:
            msg = (
                f"connector bug: table '{table}' declares key column '{col}' "
                f"but the upsert batch has columns {batch.upserts.columns}"
            )
            raise ConnectorError(msg)
    if batch.upserts.select(key).unique().height != batch.upserts.height:
        msg = (
            f"connector bug: table '{table}' upsert batch repeats a key; "
            f"each sync must send one row per {key}"
        )
        raise ConnectorError(msg)
    merged = batch.upserts
    if existing is not None and existing.height > 0:
        kept = existing.join(batch.upserts.select(key), on=key, how="anti")
        merged = pl.concat([kept, batch.upserts], how="diagonal_relaxed")
    if batch.deletes is not None and batch.deletes.height > 0:
        delete_on = batch.deletes.columns
        unknown = [c for c in delete_on if c not in merged.columns]
        if unknown:
            msg = (
                f"connector bug: table '{table}' tombstones name column(s) "
                f"{unknown} the table does not have"
            )
            raise ConnectorError(msg)
        merged = merged.join(batch.deletes.unique(), on=delete_on, how="anti")
    # Deterministic file content regardless of fetch order.
    return merged.sort(key)


def run_sync(
    connector: SystemConnector,
    root: Path,
    *,
    full_resync: bool = False,
    source_label: str | None = None,
) -> dict[str, Any]:
    """Run one sync end to end and return the sync manifest.

    Staging-then-swap: every output is built in a temporary folder first.
    Any failure before the final swap leaves the previous good sync, the
    state, and the cursor exactly as they were.
    """
    info = connector.describe()
    store = SyncStore(root, info.name)
    since = None if full_resync else store.cursor()
    secrets = _secret_values(info)

    batch = connector.sync(since)

    # Merge into state (in memory; nothing on disk changes yet).
    new_state: dict[str, pl.DataFrame] = {}
    counts: dict[str, dict[str, int]] = {}
    for table, tb in sorted(batch.tables.items()):
        existing = None if full_resync else store.read_state(table)
        merged = _merge_table(existing, tb, table)
        new_state[table] = merged
        counts[table] = {
            "rows": merged.height,
            "new_or_changed": tb.upserts.height,
            "deleted": tb.deletes.height if tb.deletes is not None else 0,
        }
    # Tables synced before but absent from this batch keep their state.
    state_dir = store.dir / "state"
    if not full_resync and state_dir.exists():
        for p in sorted(state_dir.glob("*.parquet")):
            table = p.stem
            if table not in new_state:
                kept = store.read_state(table)
                if kept is not None:
                    new_state[table] = kept
                    counts[table] = {"rows": kept.height, "new_or_changed": 0, "deleted": 0}

    # Raw files (file drop): latest accepted copy per name, carried forward.
    new_raw = {rf.name: rf for rf in batch.raw_files}
    collisions = {f"{t}.csv" for t in new_state} & set(new_raw)
    if collisions:
        msg = (
            f"connector bug: raw file(s) {sorted(collisions)} collide with a "
            f"materialized table of the same name"
        )
        raise ConnectorError(msg)
    raw_meta = {} if full_resync else store.read_raw_meta()
    raw_sources: dict[str, Path] = {}
    files_state = store.dir / "state_files"
    for name, rows in sorted(raw_meta.items()):
        kept_path = files_state / name
        if name not in new_raw and kept_path.exists():
            raw_sources[name] = kept_path
            counts[name] = {"rows": rows, "new_or_changed": 0, "deleted": 0}
    for name, rf in sorted(new_raw.items()):
        raw_sources[name] = rf.source
        raw_meta[name] = rf.rows
        counts[name] = {"rows": rf.rows, "new_or_changed": rf.rows, "deleted": 0}
    if full_resync:
        raw_meta = {name: rf.rows for name, rf in new_raw.items()}

    # Materialize canonical files in a staging folder.
    staging = store.dir / f".staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True)
    try:
        files: dict[str, dict[str, Any]] = {}
        content_hash = hashlib.sha256()
        for table in sorted(new_state):
            path = staging / f"{table}.csv"
            new_state[table].write_csv(path)
        for name, source in sorted(raw_sources.items()):
            shutil.copyfile(source, staging / name)
        for path in sorted(p for p in staging.iterdir() if p.is_file()):
            digest = _sha256_file(path)
            files[path.name] = {"bytes": path.stat().st_size, "sha256": digest}
            content_hash.update(f"{path.name}:{digest}\n".encode())

        sync_id = (
            f"{batch.window_end.replace(':', '').replace(' ', 'T')}-{content_hash.hexdigest()[:12]}"
        )
        previous = store.latest()
        if previous is not None and previous.get("content_sha256") == content_hash.hexdigest():
            # Idempotent re-run: identical content, keep the existing sync
            # folder; only the cursor moves forward.
            store._write_cursor(batch.cursor)
            shutil.rmtree(staging)
            unchanged: dict[str, Any] = dict(previous)
            unchanged["cursor_next"] = batch.cursor
            unchanged["unchanged_rerun"] = True
            # The folder is the previous sync's, but the warnings belong to
            # THIS run (e.g. "duplicate file, not processed twice").
            unchanged["warnings"] = [_scrub(w, secrets) for w in batch.warnings]
            return unchanged

        warnings = [_scrub(w, secrets) for w in batch.warnings]
        manifest: dict[str, Any] = {
            "connector": info.name,
            "connector_version": info.version,
            "framework_version": FRAMEWORK_VERSION,
            "system": info.system,
            "source": _scrub(source_label or info.system, secrets),
            "live_verification": info.live_verification,
            "sync_id": sync_id,
            "synced_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "window": {"since": batch.window_start, "until": batch.window_end},
            "cursor_used": since,
            "cursor_next": batch.cursor,
            "full_resync": full_resync,
            "records": counts,
            "files": files,
            "content_sha256": content_hash.hexdigest(),
            "warnings": warnings,
            "mapping_file": info.mapping_file,
        }
        manifest_text = json.dumps(manifest, indent=2)
        leaked = [s for s in secrets if s in manifest_text]
        if leaked:
            msg = (
                f"connector '{info.name}' tried to write a credential value into "
                f"its sync manifest; the sync was aborted and nothing was written"
            )
            raise ConnectorError(msg)
        (staging / SYNC_MANIFEST_NAME).write_text(manifest_text, encoding="utf-8")

        # Commit: sync folder first, then state, then cursor and pointer.
        store.syncs_dir.mkdir(parents=True, exist_ok=True)
        final = store.syncs_dir / sync_id
        if final.exists():  # same id from a crashed earlier run: replace it
            shutil.rmtree(final)
        os.replace(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    state_staging = store.dir / f".state-staging-{uuid.uuid4().hex[:8]}"
    state_staging.mkdir(parents=True)
    for table, frame in new_state.items():
        frame.write_parquet(state_staging / f"{table}.parquet")
    old_state = store.dir / "state"
    backup = store.dir / f".state-old-{uuid.uuid4().hex[:8]}"
    if old_state.exists():
        os.replace(old_state, backup)
    os.replace(state_staging, old_state)
    shutil.rmtree(backup, ignore_errors=True)

    # Raw-file state: the latest accepted copy per name plus its row count.
    files_staging = store.dir / f".files-staging-{uuid.uuid4().hex[:8]}"
    files_staging.mkdir(parents=True)
    final_meta = {name: raw_meta[name] for name in sorted(raw_sources)}
    for name in final_meta:
        shutil.copyfile(final / name, files_staging / name)
    (files_staging / "meta.json").write_text(json.dumps(final_meta, indent=1), encoding="utf-8")
    files_backup = store.dir / f".files-old-{uuid.uuid4().hex[:8]}"
    if files_state.exists():
        os.replace(files_state, files_backup)
    os.replace(files_staging, files_state)
    shutil.rmtree(files_backup, ignore_errors=True)

    store._write_cursor(batch.cursor)
    store._write_latest(sync_id)
    return manifest
