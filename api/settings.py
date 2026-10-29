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
    # Action center (Phase 39). users: local IDENTITY spec (name:role[+role],
    # comma-separated) — explicitly NOT authentication; real login belongs to a
    # future hosting phase. Clients send X-User with a configured name.
    actions_db: Path = field(
        default_factory=lambda: _env_path(
            "MARGIN_API_ACTIONS_DB", REPO / ".api_state" / "actions.duckdb"
        )
    )
    # "1" deletes the action store file at process start, before anything
    # opens it, so the seed produces a fresh store. For test/demo servers
    # whose review state is disposable; never the default.
    actions_reset: bool = field(
        default_factory=lambda: os.environ.get("MARGIN_API_ACTIONS_RESET") == "1"
    )
    # Directory holding engine exports (actions.parquet, action_events.parquet)
    # to seed/merge the action store from at startup; empty = bundle_dir's
    # sibling engine output is not assumed, nothing is seeded.
    actions_seed_dir: Path | None = field(
        default_factory=lambda: (
            Path(v) if (v := os.environ.get("MARGIN_API_ACTIONS_SEED")) else None
        )
    )
    # Drift monitoring (Phase 43): previous-run snapshot + latest flags.
    drift_dir: Path = field(
        default_factory=lambda: _env_path("MARGIN_API_DRIFT_DIR", REPO / ".api_state" / "drift")
    )
    # Policy rules (the price-export template lives under actions.price_export).
    rules_path: Path = field(
        default_factory=lambda: _env_path("MARGIN_API_RULES", REPO / "config" / "rules.yaml")
    )
    # Load-your-own-data workspace (polish round): uploads, drafted mappings
    # and per-upload engine runs live here. API/local mode only - the static
    # demo has no API, so this never exists in the public demo.
    uploads_dir: Path = field(
        default_factory=lambda: _env_path("MARGIN_API_UPLOADS_DIR", REPO / ".api_state" / "uploads")
    )
    # --- identity and production hardening (Run 6, Phase 82) --------------
    # "local" keeps the Phase 39 behaviour: X-User names a configured user,
    # which is identity, not authentication, and the mode name says so.
    # "production" rejects client-supplied identity entirely and requires a
    # bearer token the server issued.
    mode: str = field(default_factory=lambda: os.environ.get("MARGIN_API_MODE", "local"))
    # user_id|display name|roles|password_hash, one per line or
    # semicolon-separated. Required in production mode.
    accounts_spec: str = field(default_factory=lambda: os.environ.get("MARGIN_API_ACCOUNTS", ""))
    session_ttl_s: float = field(
        default_factory=lambda: float(os.environ.get("MARGIN_API_SESSION_TTL_S", str(12 * 3600)))
    )
    audit_log: Path = field(
        default_factory=lambda: _env_path("MARGIN_API_AUDIT_LOG", REPO / ".api_state" / "audit.log")
    )
    audit_enabled: bool = field(
        default_factory=lambda: os.environ.get("MARGIN_API_AUDIT", "1") != "0"
    )
    # Production mode will not start unless the operator declares that the
    # state directory sits on encrypted storage. The engine cannot verify
    # that from inside the process, and a silent assumption is worse than a
    # refusal: DuckDB has no at-rest encryption of its own, so uploads,
    # action history and the audit log are protected by the volume or not at
    # all. Declaring it is a decision someone has to make and own.
    storage_encrypted: bool = field(
        default_factory=lambda: os.environ.get("MARGIN_API_STORAGE_ENCRYPTED") == "1"
    )
    # Uploaded files are deleted after this many days. 0 keeps them.
    uploads_retention_days: float = field(
        default_factory=lambda: float(os.environ.get("MARGIN_API_UPLOADS_RETENTION_DAYS", "7"))
    )
    users_spec: str = field(
        default_factory=lambda: os.environ.get(
            "MARGIN_API_USERS",
            "demo-analyst:analyst,demo-approver:approver,"
            "demo-lead:analyst+approver,demo-viewer:viewer",
        )
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
