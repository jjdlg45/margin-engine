"""API configuration from environment variables (documented in README's
"Live mode" section and docs/deploy-api.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


@dataclass
class Settings:
    bundle_dir: Path = field(
        default_factory=lambda: _env_path("MARGIN_API_BUNDLE_DIR", REPO / "web" / "public" / "data")
    )
    db_path: Path = field(
        default_factory=lambda: _env_path("MARGIN_API_DB", REPO / ".api_state" / "api.duckdb")
    )
    rate_limit_per_minute: int = field(
        default_factory=lambda: int(os.environ.get("MARGIN_API_RATE_LIMIT", "240"))
    )
    rate_limit_burst: int = field(
        default_factory=lambda: int(os.environ.get("MARGIN_API_RATE_BURST", "60"))
    )
    # Replay feed (30.4): off unless configured. Speed = seconds of wall time
    # per replayed data-day.
    replay_enabled: bool = field(
        default_factory=lambda: os.environ.get("MARGIN_API_REPLAY", "0") == "1"
    )
    replay_seconds_per_day: float = field(
        default_factory=lambda: float(os.environ.get("MARGIN_API_REPLAY_SPEED", "60"))
    )
    replay_source_dir: Path = field(
        default_factory=lambda: _env_path(
            "MARGIN_API_REPLAY_SOURCE", REPO / "data" / "sample_online_retail"
        )
    )
    replay_mapping: Path = field(
        default_factory=lambda: _env_path(
            "MARGIN_API_REPLAY_MAPPING", REPO / "config" / "mappings" / "online_retail_ii.yaml"
        )
    )
    replay_work_dir: Path = field(
        default_factory=lambda: _env_path("MARGIN_API_REPLAY_WORK", REPO / ".api_state" / "replay")
    )
    # Scheduled connector refreshes (seconds; defaults: FX daily, PPI monthly).
    fx_refresh_s: float = field(
        default_factory=lambda: float(os.environ.get("MARGIN_API_FX_REFRESH_S", str(24 * 3600)))
    )
    ppi_refresh_s: float = field(
        default_factory=lambda: float(
            os.environ.get("MARGIN_API_PPI_REFRESH_S", str(30 * 24 * 3600))
        )
    )
