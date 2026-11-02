"""Run 7, Phase 90: the system-connector contract, proven with a fake.

Idempotency, cursor advance, partial-failure rollback, tombstones, full
resync, manifest completeness, run-manifest linking, credential resolution,
and the rule that no secret value appears in anything the framework writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import polars as pl
import pytest

from margin_engine.connectors.base import ConnectorError
from margin_engine.connectors.credentials import (
    CredentialStore,
    EncryptedKeyStore,
    parse_env_file,
)
from margin_engine.connectors.framework import (
    CheckResult,
    ConnectorInfo,
    SettingSpec,
    SyncBatch,
    SyncStore,
    SystemConnector,
    TableBatch,
    run_sync,
)
from margin_engine.connectors.registry import build_connector, connector_names
from margin_engine.manifest.run_manifest import linked_sync_manifest

SECRET = "sk-fake-credential-123456"  # planted; must never reach disk


class FakeConnector(SystemConnector):
    """A connector whose 'source system' is an in-memory list of rows."""

    def __init__(self, *, fail_on_sync: bool = False) -> None:
        self.rows: list[dict[str, object]] = [
            {"invoice_id": "I-1", "line": 1, "amount": "10.00", "updated": "2026-01-01"},
            {"invoice_id": "I-1", "line": 2, "amount": "4.50", "updated": "2026-01-01"},
            {"invoice_id": "I-2", "line": 1, "amount": "7.25", "updated": "2026-01-02"},
        ]
        self.customers: list[dict[str, object]] = [
            {"customer_id": "C-1", "name": "Acme Supply", "updated": "2026-01-01"},
        ]
        self.deleted_invoice_lines: list[dict[str, object]] = []
        self.fail_on_sync = fail_on_sync
        self.last_since: str | None = "UNSET"

    def describe(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="fake",
            version="1.0",
            system="Fake ERP",
            description="in-memory test double",
            produces=("invoice_lines.csv", "customers.csv"),
            required_settings=(SettingSpec(env="FAKE_TOKEN", purpose="API token", secret=True),),
            scopes=("read-only",),
            live_verification="test double; not a real system",
        )

    def check(self) -> CheckResult:
        if os.environ.get("FAKE_TOKEN"):
            return CheckResult(ok=True, message="Credentials present and the source answered.")
        return CheckResult(ok=False, message="FAKE_TOKEN is not set.")

    def sync(self, since: str | None) -> SyncBatch:
        self.last_since = since
        if self.fail_on_sync:
            msg = "Fake ERP did not answer; the previous sync is untouched."
            raise ConnectorError(msg)
        rows = [r for r in self.rows if since is None or str(r["updated"]) > since]
        custs = [c for c in self.customers if since is None or str(c["updated"]) > since]
        max_updated = max(
            [str(r["updated"]) for r in self.rows] + [str(c["updated"]) for c in self.customers],
            default=since or "1970-01-01",
        )
        tables = {
            "invoice_lines": TableBatch(
                key=("invoice_id", "line"),
                upserts=(
                    pl.DataFrame(rows)
                    if rows
                    else pl.DataFrame(
                        schema={
                            "invoice_id": pl.String,
                            "line": pl.Int64,
                            "amount": pl.String,
                            "updated": pl.String,
                        }
                    )
                ),
                deletes=(
                    pl.DataFrame(self.deleted_invoice_lines) if self.deleted_invoice_lines else None
                ),
            ),
            "customers": TableBatch(
                key=("customer_id",),
                upserts=(
                    pl.DataFrame(custs)
                    if custs
                    else pl.DataFrame(
                        schema={"customer_id": pl.String, "name": pl.String, "updated": pl.String}
                    )
                ),
            ),
        }
        return SyncBatch(
            tables=tables,
            cursor=max_updated,
            window_start=since,
            window_end=max_updated,
            warnings=[f"fetched with token {os.environ.get('FAKE_TOKEN', '')}"],
        )


@pytest.fixture
def fake_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("FAKE_TOKEN", SECRET)
    return SECRET


def _tree_text(root: Path) -> str:
    """Every byte of every file the framework wrote, as text."""
    parts = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            parts.append(p.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(parts)


def test_first_sync_materializes_canonical_files_and_manifest(
    tmp_path: Path, fake_token: str
) -> None:
    manifest = run_sync(FakeConnector(), tmp_path)
    store = SyncStore(tmp_path, "fake")
    sync_dir = store.latest_dir()
    assert sync_dir is not None
    lines = pl.read_csv(sync_dir / "invoice_lines.csv")
    assert lines.height == 3
    assert pl.read_csv(sync_dir / "customers.csv").height == 1

    # Manifest completeness: name and version, source, window, counts per
    # object, checksum per file, warnings, cursor used.
    assert manifest["connector"] == "fake"
    assert manifest["connector_version"] == "1.0"
    assert manifest["system"] == "Fake ERP"
    assert manifest["window"] == {"since": None, "until": "2026-01-02"}
    assert manifest["cursor_used"] is None
    assert manifest["cursor_next"] == "2026-01-02"
    assert manifest["records"]["invoice_lines"] == {
        "rows": 3,
        "new_or_changed": 3,
        "deleted": 0,
    }
    assert set(manifest["files"]) == {"invoice_lines.csv", "customers.csv"}
    for entry in manifest["files"].values():
        assert len(entry["sha256"]) == 64
        assert entry["bytes"] > 0
    assert manifest["warnings"], "warnings travel into the manifest"
    on_disk = json.loads((sync_dir / "sync_manifest.json").read_text(encoding="utf-8"))
    assert on_disk["sync_id"] == manifest["sync_id"]


def test_no_secret_in_any_output(tmp_path: Path, fake_token: str) -> None:
    run_sync(FakeConnector(), tmp_path)
    everything = _tree_text(tmp_path)
    assert SECRET not in everything
    assert "[redacted]" in everything  # the warning survived, scrubbed


def test_cursor_advances_and_incremental_sync_uses_it(tmp_path: Path, fake_token: str) -> None:
    connector = FakeConnector()
    run_sync(connector, tmp_path)
    assert connector.last_since is None
    store = SyncStore(tmp_path, "fake")
    assert store.cursor() == "2026-01-02"

    # A late edit and a new record arrive; the next sync sees only them but
    # the state keeps everything.
    connector.rows.append(
        {"invoice_id": "I-3", "line": 1, "amount": "99.99", "updated": "2026-01-03"}
    )
    connector.rows[0] = {
        "invoice_id": "I-1",
        "line": 1,
        "amount": "11.00",
        "updated": "2026-01-03",
    }
    manifest2 = run_sync(connector, tmp_path)
    assert connector.last_since == "2026-01-02"
    assert manifest2["records"]["invoice_lines"]["new_or_changed"] == 2
    sync_dir = SyncStore(tmp_path, "fake").latest_dir()
    assert sync_dir is not None
    lines = pl.read_csv(sync_dir / "invoice_lines.csv", infer_schema=False)
    assert lines.height == 4
    assert (
        lines.filter((pl.col("invoice_id") == "I-1") & (pl.col("line") == "1"))["amount"].item()
        == "11.00"
    )


def test_rerun_with_no_changes_is_idempotent(tmp_path: Path, fake_token: str) -> None:
    connector = FakeConnector()
    first = run_sync(connector, tmp_path)
    again = run_sync(connector, tmp_path)
    assert again["unchanged_rerun"] is True
    assert again["sync_id"] == first["sync_id"]
    store = SyncStore(tmp_path, "fake")
    assert len(list(store.syncs_dir.iterdir())) == 1, "no duplicate sync folder"


def test_partial_failure_leaves_previous_good_sync(tmp_path: Path, fake_token: str) -> None:
    connector = FakeConnector()
    good = run_sync(connector, tmp_path)
    connector.fail_on_sync = True
    connector.rows.append(
        {"invoice_id": "I-9", "line": 1, "amount": "1.00", "updated": "2026-02-01"}
    )
    with pytest.raises(ConnectorError, match="previous sync is untouched"):
        run_sync(connector, tmp_path)
    store = SyncStore(tmp_path, "fake")
    assert store.cursor() == good["cursor_next"], "cursor did not advance on failure"
    latest = store.latest()
    assert latest is not None and latest["sync_id"] == good["sync_id"]
    sync_dir = store.latest_dir()
    assert sync_dir is not None
    assert pl.read_csv(sync_dir / "invoice_lines.csv").height == 3
    assert not list(store.dir.glob(".staging-*")), "no half-written staging folder"


def test_tombstones_delete_rows_from_state(tmp_path: Path, fake_token: str) -> None:
    connector = FakeConnector()
    run_sync(connector, tmp_path)
    connector.rows = [{"invoice_id": "I-2", "line": 1, "amount": "7.25", "updated": "2026-01-04"}]
    connector.deleted_invoice_lines = [
        {"invoice_id": "I-1", "line": 1},
        {"invoice_id": "I-1", "line": 2},
    ]
    manifest = run_sync(connector, tmp_path)
    assert manifest["records"]["invoice_lines"]["deleted"] == 2
    sync_dir = SyncStore(tmp_path, "fake").latest_dir()
    assert sync_dir is not None
    lines = pl.read_csv(sync_dir / "invoice_lines.csv")
    assert lines["invoice_id"].to_list() == ["I-2"]


def test_full_resync_rebuilds_from_nothing(tmp_path: Path, fake_token: str) -> None:
    connector = FakeConnector()
    run_sync(connector, tmp_path)
    connector.rows = connector.rows[:1]  # the source lost two rows
    manifest = run_sync(connector, tmp_path, full_resync=True)
    assert connector.last_since is None
    assert manifest["full_resync"] is True
    assert manifest["records"]["invoice_lines"]["rows"] == 1


def test_run_manifest_links_the_sync(tmp_path: Path, fake_token: str) -> None:
    manifest = run_sync(FakeConnector(), tmp_path)
    sync_dir = SyncStore(tmp_path, "fake").latest_dir()
    assert sync_dir is not None
    link = linked_sync_manifest(sync_dir)
    assert link is not None
    assert link["connector"] == "fake"
    assert link["sync_id"] == manifest["sync_id"]
    assert linked_sync_manifest(tmp_path) is None  # plain export folders: no link


def test_registry_rejects_unknown_connector() -> None:
    with pytest.raises(ConnectorError, match="unknown connector"):
        build_connector("no_such_system")
    assert isinstance(connector_names(), list)


# -- credentials ---------------------------------------------------------------


def test_env_file_parsing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "PLAIN=abc\n"
        'QUOTED="with spaces"\n'
        "export EXPORTED='single'\n"
        "NOVALUE=\n"
        "broken line without equals\n",
        encoding="utf-8",
    )
    values = parse_env_file(env)
    assert values["PLAIN"] == "abc"
    assert values["QUOTED"] == "with spaces"
    assert values["EXPORTED"] == "single"
    assert values["NOVALUE"] == ""
    assert "broken line without equals" not in values


def test_credential_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A_TOKEN=from-file\nB_TOKEN=from-file\n", encoding="utf-8")
    store = CredentialStore(env_file=env_file)
    monkeypatch.setenv("A_TOKEN", "from-env")
    assert store.get("A_TOKEN") == "from-env"  # process env wins
    assert store.get("B_TOKEN") == "from-file"
    assert store.get("C_TOKEN") is None


def test_missing_message_names_each_setting(tmp_path: Path) -> None:
    info = FakeConnector().describe()
    store = CredentialStore(env_file=tmp_path / ".env")
    msg = store.missing_message(info)
    assert "FAKE_TOKEN" in msg
    assert "API token" in msg
    assert "never commit" in msg


def test_encrypted_keystore_round_trip_and_no_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MARGIN_CONNECTOR_KEY", Fernet.generate_key().decode("ascii"))
    ks = EncryptedKeyStore(tmp_path / "connectors.enc")
    ks.set("QBO_CLIENT_SECRET", SECRET)
    assert ks.get("QBO_CLIENT_SECRET") == SECRET
    assert ks.names() == ["QBO_CLIENT_SECRET"]
    raw = (tmp_path / "connectors.enc").read_bytes()
    assert SECRET.encode() not in raw, "credential is encrypted at rest"

    # The credential store falls back to the keystore when env and .env miss.
    monkeypatch.delenv("QBO_CLIENT_SECRET", raising=False)
    store = CredentialStore(env_file=tmp_path / ".env", keystore=ks)
    assert store.get("QBO_CLIENT_SECRET") == SECRET

    # A wrong key fails with a plain message, not a stack of crypto errors.
    monkeypatch.setenv("MARGIN_CONNECTOR_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(ConnectorError, match="cannot be decrypted"):
        EncryptedKeyStore(tmp_path / "connectors.enc").get("QBO_CLIENT_SECRET")


def test_keystore_requires_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARGIN_CONNECTOR_KEY", raising=False)
    with pytest.raises(ConnectorError, match="MARGIN_CONNECTOR_KEY"):
        EncryptedKeyStore(tmp_path / "connectors.enc").set("X", "y")


# -- scheduler entries ---------------------------------------------------------


def test_scheduler_connector_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from api.refresh import RefreshScheduler
    from api.settings import Settings
    from api.store import BundleStore

    monkeypatch.setenv("MARGIN_API_CONNECTORS", "filedrop:900, quickbooks_online , bad:x")
    monkeypatch.setenv("MARGIN_API_DB", str(tmp_path / "api.duckdb"))
    s = Settings()
    sched = RefreshScheduler(s, BundleStore(s.db_path))
    entries = dict(sched.connector_entries())
    assert entries["filedrop"] == 900.0
    assert entries["quickbooks_online"] == s.connector_default_refresh_s
    assert entries["bad"] == s.connector_default_refresh_s  # bad interval falls back
