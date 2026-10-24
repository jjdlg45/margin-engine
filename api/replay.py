"""Replay feed (30.4). No free source publishes live B2B invoices, so live
data is simulated HONESTLY: the Online Retail II export is fed into the
system one business day at a time. Each tick rebuilds the engine's world from
every source line dated on or before the replayed day and re-exports the web
bundle, so a tick is idempotent by construction (re-running a day cannot
double count: the world is filtered from source, never appended to), and the
state guard skips days already replayed. The UI labels the result
"historical data replayed", never live.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

import polars as pl

from api.settings import REPO, Settings
from api.store import BundleStore

LINES_FILE = "online_retail_ii.csv"
REFERENCE_FILES = ["customers.csv", "items.csv", "fx_rates.csv"]


class ReplayFeed:
    def __init__(self, settings: Settings, store: BundleStore) -> None:
        self.s = settings
        self.store = store
        self._source_lines: pl.DataFrame | None = None
        self._canonical: pl.DataFrame | None = None

    def _lines(self) -> pl.DataFrame:
        if self._source_lines is None:
            self._source_lines = pl.read_csv(
                self.s.replay_source_dir / LINES_FILE, infer_schema=False
            )
        return self._source_lines

    def _canonical_lines(self) -> pl.DataFrame:
        """The full source parsed ONCE into the canonical schema, cached on
        disk keyed by the exact source and mapping bytes (Phase 35). A tick
        then filters this frame instead of re-parsing every replayed day."""
        if self._canonical is not None:
            return self._canonical
        from margin_engine.cache import StageCache, fingerprint_file
        from margin_engine.ingest.loader import load_lines
        from margin_engine.ingest.mapping import load_mapping

        cache = StageCache(self.s.replay_work_dir / "cache")
        key = cache.key(
            "replay_canonical_lines",
            [
                fingerprint_file(self.s.replay_source_dir / LINES_FILE),
                fingerprint_file(self.s.replay_mapping),
            ],
        )
        cached = cache.get(key)
        if cached is None:
            mapping = load_mapping(self.s.replay_mapping)
            cached = load_lines(self.s.replay_source_dir, mapping)
            cache.put(key, cached)
        self._canonical = cached
        return cached

    def available_days(self) -> list[str]:
        return sorted(self._lines()["InvoiceDate"].unique().to_list())

    def last_replayed(self) -> str | None:
        return self.store.get_meta("replay_last_day")

    def replay_next_day(self) -> str | None:
        """Replay the next pending business day; returns it, or None when the
        feed is exhausted. Crash-safe ordering: the bundle is built and loaded
        BEFORE the state advances, so an interrupted tick simply re-runs."""
        last = self.last_replayed()
        pending = [d for d in self.available_days() if last is None or d > last]
        if not pending:
            return None
        day = pending[0]
        self.replay_day(day)
        return day

    def replay_day(self, day: str) -> bool:
        """Build the world as of `day` and publish it. A day at or before the
        current state is a NO-OP (idempotency guard); re-running the same day
        otherwise produces the identical world because the lines are filtered
        from source, never appended."""
        last = self.last_replayed()
        if last is not None and day <= last:
            return False

        work = self.s.replay_work_dir / "world"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        self._lines().filter(pl.col("InvoiceDate") <= day).write_csv(work / LINES_FILE)
        for f in REFERENCE_FILES:
            src = self.s.replay_source_dir / f
            if src.exists():
                shutil.copy(src, work / f)

        from datetime import date as _date

        from margin_engine.config import load_rules
        from margin_engine.report.full_run import run_full
        from margin_engine.webexport.build import build_bundle, write_bundle

        rules_path = REPO / "config" / "rules.yaml"
        out_dir = self.s.replay_work_dir / "engine_out"
        # Only new data is parsed (Phase 35): the canonical source is cached by
        # fingerprint, and the tick's world is a filter of it. The world CSV is
        # still written above so the run manifest fingerprints real input files.
        lines = self._canonical_lines().filter(pl.col("invoice_date") <= _date.fromisoformat(day))
        res = run_full(work, self.s.replay_mapping, rules_path, out_dir, lines=lines)
        # New engine-generated actions merge into the action store without
        # touching existing actions or their review history (Phase 39).
        if res.actions is not None and res.action_events is not None and res.actions.height:
            from margin_engine.actions.store import ActionStore

            ActionStore(self.s.actions_db).merge_new(res.actions, res.action_events)
        bundle = build_bundle(
            res,
            load_rules(rules_path),
            REPO,
            data_label=(
                f"Historical data replayed (Online Retail II through {day}; "
                "not live - see docs/data-sources.md)"
            ),
        )
        bundle_dir = self.s.replay_work_dir / "bundle"
        write_bundle(bundle, bundle_dir)
        self.store.load_bundle(
            bundle_dir,
            refresh_status={
                "replay": {
                    "data_as_of": day,
                    "refreshed_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "stale": False,
                }
            },
        )
        self.store.set_meta("replay_last_day", day)
        return True
