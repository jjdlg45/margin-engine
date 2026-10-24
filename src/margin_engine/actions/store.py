"""DuckDB persistence for actions (Phase 38; the API serves from this in
Phase 39). The event log is append-only: writes go through append_event's
transition validation, and the stored status is always derived from the log.

Frames move between polars and DuckDB via parquet files (the engine's
established handoff: pyarrow is not a dependency and parquet keeps exact
types).
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import duckdb
import polars as pl

from margin_engine.actions.generate import ACTIONS_SCHEMA
from margin_engine.actions.model import EVENTS_SCHEMA, append_event, empty_events, validate_log


def _load_table(con: duckdb.DuckDBPyConnection, name: str, frame: pl.DataFrame) -> None:
    with tempfile.TemporaryDirectory() as td:
        pq = Path(td) / f"{name}.parquet"
        frame.write_parquet(pq)
        con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM read_parquet(?)', [str(pq)])


def _read_table(con: duckdb.DuckDBPyConnection, sql: str) -> pl.DataFrame:
    with tempfile.TemporaryDirectory() as td:
        pq = Path(td) / "out.parquet"
        con.execute(f"COPY ({sql}) TO '{pq}' (FORMAT parquet)")
        return pl.read_parquet(pq)


class ActionStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path

    def _tables(self, con: duckdb.DuckDBPyConnection) -> set[str]:
        return {r[0] for r in con.execute("SHOW TABLES").fetchall()}

    def seed(self, actions: pl.DataFrame, events: pl.DataFrame, *, replace: bool = False) -> bool:
        """Load a generated action set. Idempotent: an already-seeded store is
        left untouched unless replace=True (re-seeding resets the log to the
        generated birth events)."""
        validate_log(events)
        con = duckdb.connect(str(self._path))
        try:
            if "action_events" in self._tables(con) and not replace:
                n = con.execute("SELECT count(*) FROM action_events").fetchone()
                if n and int(n[0]) > 0:
                    return False
            _load_table(con, "actions", actions)
            _load_table(con, "action_events", events)
            return True
        finally:
            con.close()

    def actions(self) -> pl.DataFrame:
        con = duckdb.connect(str(self._path), read_only=True)
        try:
            if "actions" not in self._tables(con):
                return pl.DataFrame(schema=ACTIONS_SCHEMA)
            return _read_table(con, "SELECT * FROM actions").cast(ACTIONS_SCHEMA)  # type: ignore[arg-type]
        finally:
            con.close()

    def events(self) -> pl.DataFrame:
        con = duckdb.connect(str(self._path), read_only=True)
        try:
            if "action_events" not in self._tables(con):
                return empty_events()
            out = _read_table(con, "SELECT * FROM action_events ORDER BY action_id, seq")
            return out.cast(EVENTS_SCHEMA)  # type: ignore[arg-type]
        finally:
            con.close()

    def transition(
        self,
        action_id: str,
        *,
        who: str,
        to_status: str,
        note: str = "",
        at: dt.datetime | None = None,
    ) -> pl.DataFrame:
        """Append one validated transition and persist it; returns the new log.
        append_event raises TransitionError on anything the lifecycle forbids
        (including a repeat of the current status), which the API layer turns
        into an idempotent no-op for repeated requests."""
        events = self.events()
        new_log = append_event(
            events,
            action_id,
            who=who,
            at=at if at is not None else dt.datetime.now(dt.UTC),
            to_status=to_status,
            note=note,
        )
        new_row = new_log.tail(1)
        con = duckdb.connect(str(self._path))
        try:
            with tempfile.TemporaryDirectory() as td:
                pq = Path(td) / "row.parquet"
                new_row.write_parquet(pq)
                con.execute("INSERT INTO action_events SELECT * FROM read_parquet(?)", [str(pq)])
        finally:
            con.close()
        return new_log
