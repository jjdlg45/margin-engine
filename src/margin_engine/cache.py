"""Fingerprint-keyed stage-output cache (Phase 35).

A stage output (a Polars frame) is stored as parquet under a key derived from
the exact bytes of its inputs: source-file digests plus the mapping and rules
digests plus the engine version. Any change to any input changes the key, so a
hit can only ever return the frame the same code would recompute — the cache
can go stale in size, never in content. It is OFF unless a caller passes a
cache directory (the CLI exposes --cache-dir; the replay feed uses it so a
tick re-parses only data it has never seen).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl

from margin_engine import __version__


def fingerprint_file(path: Path) -> str:
    """Content digest of one input file (streamed, constant memory)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class StageCache:
    def __init__(self, cache_dir: Path) -> None:
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def key(self, stage: str, parts: list[str]) -> str:
        h = hashlib.sha256()
        h.update(f"v={__version__};stage={stage}".encode())
        for part in parts:
            h.update(b"\x00")
            h.update(part.encode())
        return h.hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.parquet"

    def get(self, key: str) -> pl.DataFrame | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return pl.read_parquet(path)
        except pl.exceptions.PolarsError:
            # A torn write from an interrupted run is a miss, never an error.
            path.unlink(missing_ok=True)
            return None

    def get_meta(self, key: str) -> dict[str, int] | None:
        """Small sidecar metadata stored with put(meta=...), e.g. the blank-row
        counts the original parse recorded while dropping those rows."""
        path = self._path(key).with_suffix(".meta.json")
        if not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return {str(k): int(v) for k, v in loaded.items()}

    def put(self, key: str, frame: pl.DataFrame, meta: dict[str, int] | None = None) -> None:
        if meta is not None:
            mpath = self._path(key).with_suffix(".meta.json")
            mpath.write_text(json.dumps(meta), encoding="utf-8")
        tmp = self._path(key).with_suffix(".tmp")
        frame.write_parquet(tmp)
        tmp.replace(self._path(key))
