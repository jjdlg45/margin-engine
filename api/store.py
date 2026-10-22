"""DuckDB-backed bundle store (30.2).

The engine's `export-web` bundle is loaded into DuckDB (pages as documents,
table rows relationally, explanations keyed) with a version stamp. The API
serves from an in-memory snapshot of the current version; ETag/Last-Modified
come from the version stamp, so clients cache until the next refresh swaps
the snapshot atomically.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from api.filters import GUIDANCE_NULLABLE

PAGES = [
    "overview",
    "waterfall",
    "customers",
    "segments",
    "guidance",
    "sensitivity",
    "opportunity",
    "scenarios",
    "methodology",
    "modules",
]
TABLES = ["guidance", "opportunities", "customers"]


def _read_shards(bundle: Path, rel: str) -> list[dict[str, Any]]:
    """Follow the static bundle's "next"-linked shard chain."""
    rows: list[dict[str, Any]] = []
    nxt: str | None = rel
    while nxt:
        payload = json.loads((bundle / nxt).read_text(encoding="utf-8"))
        rows.extend(payload["items"])
        nxt = payload.get("next")
    return rows


def _read_keyed_shards(bundle: Path, rel: str) -> dict[str, str]:
    out: dict[str, str] = {}
    nxt: str | None = rel
    while nxt:
        payload = json.loads((bundle / nxt).read_text(encoding="utf-8"))
        out.update(payload["items"])
        nxt = payload.get("next")
    return out


@dataclass
class Snapshot:
    version: str
    loaded_utc: datetime
    app: dict[str, Any]
    pages: dict[str, dict[str, Any]]
    tables: dict[str, list[dict[str, Any]]]
    explanations: dict[str, str]
    refresh_status: dict[str, Any] = field(default_factory=dict)

    @property
    def etag(self) -> str:
        return f'"{self.version}"'

    @property
    def last_modified(self) -> str:
        return self.loaded_utc.strftime("%a, %d %b %Y %H:%M:%S GMT")


class BundleStore:
    """Loads a bundle into DuckDB and keeps the served snapshot in memory.
    reload() is atomic from the readers' perspective."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        with self._conn() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS documents("
                "version TEXT, kind TEXT, name TEXT, body TEXT, loaded_utc TIMESTAMP)"
            )
            con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")

    def _conn(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self._db_path))

    # -- loading -----------------------------------------------------------

    def load_bundle(self, bundle_dir: Path, refresh_status: dict[str, Any] | None = None) -> str:
        """Read the exported bundle, persist it in DuckDB under a content
        version, and swap the in-memory snapshot."""
        app = json.loads((bundle_dir / "app.json").read_text(encoding="utf-8"))
        pages = {
            p: json.loads((bundle_dir / f"{p}.json").read_text(encoding="utf-8")) for p in PAGES
        }
        tables: dict[str, list[dict[str, Any]]] = {
            t: _read_shards(bundle_dir, f"tables/{t}.json") for t in TABLES
        }
        for r in tables["guidance"]:
            for k in GUIDANCE_NULLABLE:
                r.setdefault(k, None)
        explanations = _read_keyed_shards(bundle_dir, "tables/guidance_explanations.json")

        digest = hashlib.sha256()
        for name in ["app.json", *(f"{p}.json" for p in PAGES)]:
            digest.update((bundle_dir / name).read_bytes())
        for t in TABLES:
            digest.update(json.dumps(tables[t], sort_keys=True).encode())
        version = digest.hexdigest()[:16]
        now = datetime.now(UTC)

        with self._conn() as con:
            con.execute("DELETE FROM documents WHERE version = ?", [version])
            rows = [
                (version, "app", "app", json.dumps(app), now),
                *((version, "page", p, json.dumps(pages[p]), now) for p in PAGES),
                *((version, "table", t, json.dumps(tables[t]), now) for t in TABLES),
                (version, "explanations", "guidance", json.dumps(explanations), now),
            ]
            con.executemany(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            con.execute("INSERT OR REPLACE INTO meta VALUES ('current_version', ?)", [version])

        snap = Snapshot(
            version=version,
            loaded_utc=now,
            app=app,
            pages=pages,
            tables=tables,
            explanations=explanations,
            refresh_status=refresh_status or {},
        )
        with self._lock:
            self._snapshot = snap
        return version

    def load_from_db(self) -> bool:
        """Restore the last persisted version (service restart without a
        bundle re-export). Returns False when the DB is empty."""
        with self._conn() as con:
            cur = con.execute("SELECT value FROM meta WHERE key = 'current_version'").fetchone()
            if not cur:
                return False
            version = str(cur[0])
            docs = con.execute(
                "SELECT kind, name, body, loaded_utc FROM documents WHERE version = ?",
                [version],
            ).fetchall()
        if not docs:
            return False
        app: dict[str, Any] = {}
        pages: dict[str, dict[str, Any]] = {}
        tables: dict[str, list[dict[str, Any]]] = {}
        explanations: dict[str, str] = {}
        loaded = datetime.now(UTC)
        for kind, name, body, loaded_utc in docs:
            loaded = loaded_utc.replace(tzinfo=UTC) if loaded_utc.tzinfo is None else loaded_utc
            if kind == "app":
                app = json.loads(body)
            elif kind == "page":
                pages[name] = json.loads(body)
            elif kind == "table":
                tables[name] = json.loads(body)
            elif kind == "explanations":
                explanations = json.loads(body)
        with self._lock:
            self._snapshot = Snapshot(
                version=version,
                loaded_utc=loaded,
                app=app,
                pages=pages,
                tables=tables,
                explanations=explanations,
            )
        return True

    # -- meta state (replay bookkeeping etc.) --------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._conn() as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as con:
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])

    # -- serving -------------------------------------------------------------

    @property
    def snapshot(self) -> Snapshot:
        with self._lock:
            if self._snapshot is None:
                msg = "no bundle loaded"
                raise RuntimeError(msg)
            return self._snapshot

    def set_refresh_status(self, status: dict[str, Any]) -> None:
        with self._lock:
            if self._snapshot is not None:
                self._snapshot.refresh_status = status
