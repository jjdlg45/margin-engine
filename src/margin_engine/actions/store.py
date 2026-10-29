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
from margin_engine.actions.revalidate import REVALIDATION_SCHEMA
from margin_engine.actions.tracking import OUTCOMES_SCHEMA


class ConcurrencyError(RuntimeError):
    """Two transitions raced on one action: the loser wrote nothing (Run 6,
    Phase 69). The message names the expected and the found sequence."""


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
        if not self._path.exists():
            return pl.DataFrame(schema=ACTIONS_SCHEMA)
        con = duckdb.connect(str(self._path), read_only=True)
        try:
            if "actions" not in self._tables(con):
                return pl.DataFrame(schema=ACTIONS_SCHEMA)
            out = _read_table(con, "SELECT * FROM actions")
        finally:
            con.close()
        # Light forward migration: a store written by an older engine may lack
        # newly added schema columns (e.g. uom, Phase 40); they read as null.
        missing = [c for c in ACTIONS_SCHEMA if c not in out.columns]
        if missing:
            out = out.with_columns([pl.lit(None).cast(ACTIONS_SCHEMA[c]).alias(c) for c in missing])
        return out.select(list(ACTIONS_SCHEMA)).cast(ACTIONS_SCHEMA)  # type: ignore[arg-type]

    def events(self) -> pl.DataFrame:
        if not self._path.exists():
            return empty_events()
        con = duckdb.connect(str(self._path), read_only=True)
        try:
            if "action_events" not in self._tables(con):
                return empty_events()
            out = _read_table(con, "SELECT * FROM action_events ORDER BY action_id, seq")
            return out.cast(EVENTS_SCHEMA)  # type: ignore[arg-type]
        finally:
            con.close()

    def get_action(self, action_id: str) -> dict[str, object] | None:
        actions = self.actions()
        row = actions.filter(pl.col("action_id") == action_id)
        return row.to_dicts()[0] if row.height else None

    def merge_new(self, actions: pl.DataFrame, events: pl.DataFrame) -> int:
        """Add actions whose id is not yet in the store (plus their birth
        events) WITHOUT touching existing actions or their review history —
        a fresh engine run never erases human decisions. Returns how many
        were added."""
        validate_log(events)
        existing = self.actions()
        if existing.height == 0 and self.events().height == 0:
            self.seed(actions, events, replace=True)
            return actions.height
        known = set(existing["action_id"].to_list())
        new_actions = actions.filter(~pl.col("action_id").is_in(sorted(known)))
        if new_actions.height == 0:
            return 0
        # An older on-disk table may lack newly added schema columns; rewrite
        # it in the current (forward-migrated) schema before inserting.
        con = duckdb.connect(str(self._path))
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info('actions')").fetchall()}
            if cols != set(ACTIONS_SCHEMA):
                _load_table(con, "actions", existing)
        finally:
            con.close()
        new_ids = set(new_actions["action_id"].to_list())
        new_events = events.filter(pl.col("action_id").is_in(sorted(new_ids)))
        con = duckdb.connect(str(self._path))
        try:
            with tempfile.TemporaryDirectory() as td:
                a_pq = Path(td) / "a.parquet"
                e_pq = Path(td) / "e.parquet"
                new_actions.write_parquet(a_pq)
                new_events.write_parquet(e_pq)
                con.execute("INSERT INTO actions SELECT * FROM read_parquet(?)", [str(a_pq)])
                con.execute("INSERT INTO action_events SELECT * FROM read_parquet(?)", [str(e_pq)])
        finally:
            con.close()
        return new_actions.height

    EDITS_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {  # noqa: RUF012
        "action_id": pl.String,
        "seq": pl.Int64,
        "who": pl.String,
        "at": pl.Datetime("us", "UTC"),
        "field": pl.String,
        "old_value": pl.String,
        "new_value": pl.String,
    }

    def edits(self, action_id: str | None = None) -> pl.DataFrame:
        if not self._path.exists():
            return pl.DataFrame(schema=self.EDITS_SCHEMA)
        con = duckdb.connect(str(self._path), read_only=True)
        try:
            if "action_edits" not in self._tables(con):
                return pl.DataFrame(schema=self.EDITS_SCHEMA)
            sql = "SELECT * FROM action_edits"
            if action_id is not None:
                safe = action_id.replace("'", "''")
                sql += f" WHERE action_id = '{safe}'"
            out = _read_table(con, sql + " ORDER BY action_id, seq")
            return out.cast(self.EDITS_SCHEMA)  # type: ignore[arg-type]
        finally:
            con.close()

    def apply_edit(
        self,
        action_id: str,
        *,
        who: str,
        field: str,
        value: object,
        at: dt.datetime | None = None,
    ) -> None:
        """Update one action field and record the edit in the audit table.
        Policy checks (role, editability, floor) happen in the review layer."""
        action = self.get_action(action_id)
        if action is None:
            msg = f"unknown action {action_id!r}"
            raise KeyError(msg)
        old = action.get(field)
        prior = self.edits(action_id)
        row = pl.DataFrame(
            {
                "action_id": [action_id],
                "seq": [prior.height],
                "who": [who],
                "at": [at if at is not None else dt.datetime.now(dt.UTC)],
                "field": [field],
                "old_value": [None if old is None else str(old)],
                "new_value": [None if value is None else str(value)],
            },
            schema=self.EDITS_SCHEMA,
        )
        con = duckdb.connect(str(self._path))
        try:
            if "action_edits" not in self._tables(con):
                _load_table(con, "action_edits", pl.DataFrame(schema=self.EDITS_SCHEMA))
            with tempfile.TemporaryDirectory() as td:
                pq = Path(td) / "edit.parquet"
                row.write_parquet(pq)
                con.execute("INSERT INTO action_edits SELECT * FROM read_parquet(?)", [str(pq)])
            safe = action_id.replace("'", "''")
            con.execute(f"UPDATE actions SET \"{field}\" = ? WHERE action_id = '{safe}'", [value])
        finally:
            con.close()

    def add_action(self, action: pl.DataFrame, birth: pl.DataFrame) -> None:
        """Insert one manually proposed action with its birth event."""
        validate_log(birth)
        con = duckdb.connect(str(self._path))
        try:
            with tempfile.TemporaryDirectory() as td:
                a_pq = Path(td) / "a.parquet"
                e_pq = Path(td) / "e.parquet"
                action.write_parquet(a_pq)
                birth.write_parquet(e_pq)
                con.execute("INSERT INTO actions SELECT * FROM read_parquet(?)", [str(a_pq)])
                con.execute("INSERT INTO action_events SELECT * FROM read_parquet(?)", [str(e_pq)])
        finally:
            con.close()

    def save_outcomes(self, outcomes: pl.DataFrame) -> None:
        """Replace the outcomes table (recomputable from the ledger + log)."""
        con = duckdb.connect(str(self._path))
        try:
            _load_table(con, "action_outcomes", outcomes)
        finally:
            con.close()

    def outcomes(self) -> pl.DataFrame:
        if not self._path.exists():
            return pl.DataFrame(schema=OUTCOMES_SCHEMA)
        con = duckdb.connect(str(self._path), read_only=True)
        try:
            if "action_outcomes" not in self._tables(con):
                return pl.DataFrame(schema=OUTCOMES_SCHEMA)
            out = _read_table(con, "SELECT * FROM action_outcomes ORDER BY action_id")
        finally:
            con.close()
        return out.cast(OUTCOMES_SCHEMA)  # type: ignore[arg-type]

    def transition(
        self,
        action_id: str,
        *,
        who: str,
        to_status: str,
        note: str = "",
        at: dt.datetime | None = None,
        expected_seq: int | None = None,
    ) -> pl.DataFrame:
        """Append one validated transition and persist it; returns the new log.
        append_event raises TransitionError on anything the lifecycle forbids
        (including a repeat of the current status), which the API layer turns
        into an idempotent no-op for repeated requests.

        Concurrency (Run 6, Phase 69): `expected_seq` is the caller's view of
        the action's next event sequence number. The check and the insert run
        inside ONE DuckDB transaction with the row count re-read under that
        transaction, so two simultaneous transitions cannot both win: the
        loser sees a mismatch, nothing is written, and ConcurrencyError names
        the sequence it expected and the one it found.
        """
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
        seq = int(new_row["seq"][0])
        con = duckdb.connect(str(self._path))
        try:
            with tempfile.TemporaryDirectory() as td:
                pq = Path(td) / "row.parquet"
                new_row.write_parquet(pq)
                con.execute("BEGIN TRANSACTION")
                try:
                    found = con.execute(
                        "SELECT count(*) FROM action_events WHERE action_id = ?", [action_id]
                    ).fetchone()
                    n_now = int(found[0]) if found else 0
                    if expected_seq is not None and expected_seq != n_now:
                        msg = (
                            f"action {action_id}: expected event sequence {expected_seq} "
                            f"but the log is at {n_now}; another change landed first, "
                            "so nothing was written. Reload and retry."
                        )
                        raise ConcurrencyError(msg)
                    if n_now != seq:
                        msg = (
                            f"action {action_id}: the log moved to sequence {n_now} while "
                            f"this transition was being prepared (sequence {seq}); "
                            "nothing was written. Reload and retry."
                        )
                        raise ConcurrencyError(msg)
                    con.execute(
                        "INSERT INTO action_events SELECT * FROM read_parquet(?)", [str(pq)]
                    )
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
        finally:
            con.close()
        return new_log

    # --- revalidation audit (Run 6, Phase 69) --------------------------------

    def revalidations(self, action_id: str | None = None) -> pl.DataFrame:
        if not self._path.exists():
            return pl.DataFrame(schema=REVALIDATION_SCHEMA)
        con = duckdb.connect(str(self._path), read_only=True)
        try:
            if "action_revalidations" not in self._tables(con):
                return pl.DataFrame(schema=REVALIDATION_SCHEMA)
            sql = "SELECT * FROM action_revalidations"
            if action_id is not None:
                safe = action_id.replace("'", "''")
                sql += f" WHERE action_id = '{safe}'"
            out = _read_table(con, sql + ' ORDER BY action_id, "at", seq')
            return out.cast(REVALIDATION_SCHEMA)  # type: ignore[arg-type]
        finally:
            con.close()

    def record_revalidation(self, rows: pl.DataFrame) -> None:
        """Append revalidation audit rows (both versions of every difference)."""
        if rows.height == 0:
            return
        con = duckdb.connect(str(self._path))
        try:
            if "action_revalidations" not in self._tables(con):
                _load_table(con, "action_revalidations", pl.DataFrame(schema=REVALIDATION_SCHEMA))
            with tempfile.TemporaryDirectory() as td:
                pq = Path(td) / "rev.parquet"
                rows.write_parquet(pq)
                con.execute(
                    "INSERT INTO action_revalidations SELECT * FROM read_parquet(?)", [str(pq)]
                )
        finally:
            con.close()

    # --- export journal (Run 6, Phase 69) ------------------------------------

    EXPORT_JOURNAL_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {  # noqa: RUF012
        "export_id": pl.String,
        "at": pl.Datetime("us", "UTC"),
        "who": pl.String,
        "path": pl.String,
        "action_ids": pl.String,  # ';'-joined
        "committed": pl.Boolean,
    }

    def export_journal(self) -> pl.DataFrame:
        if not self._path.exists():
            return pl.DataFrame(schema=self.EXPORT_JOURNAL_SCHEMA)
        con = duckdb.connect(str(self._path), read_only=True)
        try:
            if "action_export_journal" not in self._tables(con):
                return pl.DataFrame(schema=self.EXPORT_JOURNAL_SCHEMA)
            out = _read_table(con, 'SELECT * FROM action_export_journal ORDER BY "at", export_id')
            return out.cast(self.EXPORT_JOURNAL_SCHEMA)  # type: ignore[arg-type]
        finally:
            con.close()

    def journal_export(
        self, export_id: str, *, who: str, path: str, action_ids: list[str], at: dt.datetime
    ) -> None:
        """Record an export INTENT before the file is written, so a crash
        between the write and the status updates is recoverable."""
        row = pl.DataFrame(
            {
                "export_id": [export_id],
                "at": [at],
                "who": [who],
                "path": [path],
                "action_ids": [";".join(action_ids)],
                "committed": [False],
            },
            schema=self.EXPORT_JOURNAL_SCHEMA,
        )
        con = duckdb.connect(str(self._path))
        try:
            if "action_export_journal" not in self._tables(con):
                _load_table(
                    con, "action_export_journal", pl.DataFrame(schema=self.EXPORT_JOURNAL_SCHEMA)
                )
            with tempfile.TemporaryDirectory() as td:
                pq = Path(td) / "j.parquet"
                row.write_parquet(pq)
                con.execute(
                    "INSERT INTO action_export_journal SELECT * FROM read_parquet(?)", [str(pq)]
                )
        finally:
            con.close()

    def commit_export(self, export_id: str) -> None:
        con = duckdb.connect(str(self._path))
        try:
            if "action_export_journal" not in self._tables(con):
                return
            safe = export_id.replace("'", "''")
            con.execute(
                f"UPDATE action_export_journal SET committed = TRUE WHERE export_id = '{safe}'"
            )
        finally:
            con.close()
