"""Phase 35: fingerprint-keyed stage cache and replay parse reuse.

The cache can only return what the same code would recompute: its key covers
the exact input bytes, the mapping bytes and the engine version. These tests
prove the hit path returns the identical frame (blank-row counts included),
that any input change misses, and that the replay feed parses its source once.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from polars.testing import assert_frame_equal

from margin_engine.cache import StageCache, fingerprint_file
from margin_engine.config import load_rules
from margin_engine.ledger.pipeline import run_pipeline

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample" / "erp"
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"
RULES = REPO / "config" / "rules.yaml"


def test_cache_roundtrip_and_torn_write(tmp_path: Path) -> None:
    cache = StageCache(tmp_path)
    frame = pl.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    key = cache.key("stage", ["abc"])
    assert cache.get(key) is None
    cache.put(key, frame, meta={"f.csv": 3})
    got = cache.get(key)
    assert got is not None
    assert_frame_equal(got, frame)
    assert cache.get_meta(key) == {"f.csv": 3}
    # A torn write (truncated parquet) is a miss, never an error.
    (tmp_path / f"{key}.parquet").write_bytes(b"not parquet")
    assert cache.get(key) is None


def test_cache_key_covers_inputs(tmp_path: Path) -> None:
    f = tmp_path / "input.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    cache = StageCache(tmp_path / "cache")
    k1 = cache.key("s", [fingerprint_file(f)])
    f.write_text("a,b\n1,3\n", encoding="utf-8")
    k2 = cache.key("s", [fingerprint_file(f)])
    assert k1 != k2
    assert cache.key("other_stage", [fingerprint_file(f)]) != k2


def test_pipeline_cache_hit_is_identical(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    rules = load_rules(RULES)
    cold = run_pipeline(SAMPLE, MAPPING, rules, cache_dir=tmp_path)
    # Second run must not parse at all: a poisoned loader proves the hit.
    import margin_engine.ledger.pipeline as pipeline_mod

    def _boom(*args: object, **kwargs: object) -> pl.DataFrame:
        msg = "load_lines called despite a warm cache"
        raise AssertionError(msg)

    monkeypatch.setattr(pipeline_mod, "load_lines", _boom)
    warm = run_pipeline(SAMPLE, MAPPING, rules, cache_dir=tmp_path)
    assert_frame_equal(cold.ledger, warm.ledger)
    assert cold.blank_rows_dropped == warm.blank_rows_dropped
    assert warm.reconciliation.all_pass


def test_replay_parses_source_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sys

    sys.path.insert(0, str(REPO))
    from api.replay import ReplayFeed
    from api.settings import Settings
    from api.store import BundleStore

    source = REPO / "data" / "sample_online_retail"
    settings = Settings(
        replay_source_dir=source,
        replay_mapping=REPO / "config" / "mappings" / "online_retail_ii.yaml",
        replay_work_dir=tmp_path / "replay",
        db_path=tmp_path / "api.duckdb",
    )
    store = BundleStore(settings.db_path)
    feed = ReplayFeed(settings, store)
    first = feed._canonical_lines()
    assert first.height > 0

    # A fresh feed (fresh process, same inputs) must hit the disk cache.
    import margin_engine.ingest.loader as loader_mod

    def _boom(*args: object, **kwargs: object) -> pl.DataFrame:
        msg = "load_lines called despite a warm replay cache"
        raise AssertionError(msg)

    monkeypatch.setattr(loader_mod, "load_lines", _boom)
    feed2 = ReplayFeed(settings, store)
    again = feed2._canonical_lines()
    assert_frame_equal(first, again)
