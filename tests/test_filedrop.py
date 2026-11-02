"""Run 7, Phase 91: the file-drop connector.

Watched-folder and SFTP pick-up, completeness rules (done marker / stable
size), duplicate rejection, wrong-format quarantine with a plain-English
reason, permission errors, read-only behaviour toward the SFTP source, and
proof that the engine ingests the materialized folder unchanged.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from margin_engine.connectors.base import ConnectorError
from margin_engine.connectors.credentials import CredentialStore
from margin_engine.connectors.filedrop import FileDropConnector, _WatchedFolder, load_rules
from margin_engine.connectors.framework import SyncStore, run_sync
from margin_engine.ingest.loader import load_lines, load_reference_table
from margin_engine.ingest.mapping import load_mapping
from tests.fixtures.builder import FixtureWorld
from tests.sftp_server import InProcessSftpServer

MAPPING = "config/mappings/synth_erp.yaml"


def _write_world(folder: Path) -> None:
    """A tiny hand-checkable export in the synth ERP format."""
    world = FixtureWorld(tmp=folder)
    world.add_item("ITEM1")
    world.add_line(INV_NO="INV1", CUST_NO="C1", ITEM_NO="ITEM1", QTY_SHP="10", EXT_NET="100.00")
    world.add_line(INV_NO="INV2", CUST_NO="C2", ITEM_NO="ITEM1", QTY_SHP="4", EXT_NET="42.00")
    world.write()


@pytest.fixture
def drop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A watched folder plus connector env pointing at tmp dirs."""
    watch = tmp_path / "drop"
    watch.mkdir()
    monkeypatch.setenv("MARGIN_FILEDROP_DIR", str(watch))
    monkeypatch.setenv("MARGIN_FILEDROP_WORK", str(tmp_path / "work"))
    monkeypatch.setenv("MARGIN_FILEDROP_SETTLE_SECONDS", "0")
    monkeypatch.delenv("MARGIN_FILEDROP_SFTP_HOST", raising=False)
    return watch


def _connector() -> FileDropConnector:
    return FileDropConnector(CredentialStore())


def test_rules_load_and_describe() -> None:
    targets = load_rules(Path("config/filedrop.yaml"))
    assert {t.served_name for t in targets} == {"invoice_lines.csv", "customers.csv", "items.csv"}
    info = _connector().describe()
    assert info.name == "filedrop"
    assert "invoice_lines.csv" in info.produces


def test_watched_folder_end_to_end(drop: Path, tmp_path: Path) -> None:
    staging = tmp_path / "export"
    _write_world(staging)
    shutil.copyfile(staging / "invoice_lines.csv", drop / "invoice_lines_20260101.csv")
    shutil.copyfile(staging / "items.csv", drop / "items_20260101.csv")

    state_root = tmp_path / "state"
    manifest = run_sync(_connector(), state_root)
    assert manifest["records"]["invoice_lines.csv"]["rows"] == 2
    assert manifest["records"]["items.csv"]["rows"] == 1

    sync_dir = SyncStore(state_root, "filedrop").latest_dir()
    assert sync_dir is not None
    # Byte-identical pass-through: the dropped file is served verbatim.
    assert (sync_dir / "invoice_lines.csv").read_bytes() == (
        staging / "invoice_lines.csv"
    ).read_bytes()

    # The engine ingests the materialized folder unchanged.
    lines = load_lines(sync_dir, load_mapping(MAPPING))
    assert lines.height == 2

    # The originals moved to the drop folder's archive; the folder is clean.
    assert not (drop / "invoice_lines_20260101.csv").exists()
    assert len(list((drop / "archive").iterdir())) == 2


def test_incomplete_file_waits(drop: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file whose size changes during the settle window is not picked up."""
    staging = tmp_path / "export"
    _write_world(staging)
    partial = drop / "invoice_lines_partial.csv"
    partial.write_text("LINE_KEY,INV_NO\n", encoding="utf-8")

    grown = {"done": False}
    real_stat = _WatchedFolder.stat_size

    def growing_stat(self: _WatchedFolder, name: str) -> int:
        if name == partial.name and not grown["done"]:
            grown["done"] = True
            return real_stat(self, name) + 100  # the writer added bytes meanwhile
        return real_stat(self, name)

    monkeypatch.setattr(_WatchedFolder, "stat_size", growing_stat)
    manifest = run_sync(_connector(), tmp_path / "state")
    assert any("still being written" in w for w in manifest["warnings"])
    assert partial.exists(), "the partial file stays in place for the next sync"

    # With a done marker it is picked up even if it would otherwise wait.
    shutil.copyfile(staging / "invoice_lines.csv", drop / "invoice_lines_2.csv")
    (drop / "invoice_lines_2.csv.done").touch()
    partial.unlink()
    manifest2 = run_sync(_connector(), tmp_path / "state")
    assert manifest2["records"]["invoice_lines.csv"]["rows"] == 2
    assert not (drop / "invoice_lines_2.csv.done").exists()


def test_duplicate_file_never_processed_twice(drop: Path, tmp_path: Path) -> None:
    staging = tmp_path / "export"
    _write_world(staging)
    shutil.copyfile(staging / "invoice_lines.csv", drop / "invoice_lines_a.csv")
    run_sync(_connector(), tmp_path / "state")
    # The same bytes arrive again under a new name.
    shutil.copyfile(staging / "invoice_lines.csv", drop / "invoice_lines_b.csv")
    manifest = run_sync(_connector(), tmp_path / "state")
    assert any("not processed twice" in w for w in manifest["warnings"])
    assert manifest.get("unchanged_rerun") is True, "identical content keeps the previous sync"
    work_archive = Path(tmp_path / "work" / "archive")
    assert any("duplicate" in p.name for p in work_archive.iterdir())


def test_wrong_format_quarantined_with_reason(drop: Path, tmp_path: Path) -> None:
    (drop / "invoice_lines_bad.csv").write_text("TOTALLY,WRONG,HEADERS\n1,2,3\n", encoding="utf-8")
    (drop / "mystery.dat").write_text("not matched by any rule\n", encoding="utf-8")
    manifest = run_sync(_connector(), tmp_path / "state")
    assert manifest["records"] == {}
    quarantine = drop / "quarantine"
    reasons = sorted(p.name for p in quarantine.iterdir())
    assert any("invoice_lines_bad.csv.reason.txt" in n for n in reasons)
    assert any("mystery.dat.reason.txt" in n for n in reasons)
    reason_text = next(
        p.read_text(encoding="utf-8")
        for p in quarantine.iterdir()
        if "bad" in p.name and p.suffix == ".txt"
    )
    assert "missing column" in reason_text
    assert "LINE_KEY" in reason_text
    no_rule_text = next(
        p.read_text(encoding="utf-8")
        for p in quarantine.iterdir()
        if "mystery" in p.name and p.suffix == ".txt"
    )
    assert "No pick-up rule matches" in no_rule_text


def test_permission_error_leaves_file_for_next_sync(
    drop: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "export"
    _write_world(staging)
    shutil.copyfile(staging / "invoice_lines.csv", drop / "invoice_lines_locked.csv")

    def deny(self: _WatchedFolder, name: str, dest: Path) -> None:
        raise PermissionError(f"Permission denied: {name}")

    with monkeypatch.context() as m:
        m.setattr(_WatchedFolder, "fetch", deny)
        manifest = run_sync(_connector(), tmp_path / "state")
    assert any("could not be read" in w for w in manifest["warnings"])
    assert (drop / "invoice_lines_locked.csv").exists(), "left in place, not quarantined"
    manifest2 = run_sync(_connector(), tmp_path / "state")
    assert manifest2["records"]["invoice_lines.csv"]["rows"] == 2


def test_check_reports_plain_english(drop: Path) -> None:
    result = _connector().check()
    assert result.ok
    assert "reachable" in result.message
    assert "rule" in result.message


def test_check_fails_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARGIN_FILEDROP_DIR", raising=False)
    monkeypatch.delenv("MARGIN_FILEDROP_SFTP_HOST", raising=False)
    monkeypatch.setenv("MARGIN_FILEDROP_WORK", "unused")
    result = _connector().check()
    assert not result.ok
    assert "MARGIN_FILEDROP_DIR" in result.message


# -- SFTP ------------------------------------------------------------------------


@pytest.fixture
def sftp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[InProcessSftpServer]:
    remote = tmp_path / "remote"
    remote.mkdir()
    keys = tmp_path / "keys"
    keys.mkdir()
    server = InProcessSftpServer(remote, keys)
    server.start()
    monkeypatch.delenv("MARGIN_FILEDROP_DIR", raising=False)
    monkeypatch.setenv("MARGIN_FILEDROP_SFTP_HOST", "127.0.0.1")
    monkeypatch.setenv("MARGIN_FILEDROP_SFTP_PORT", str(server.port))
    monkeypatch.setenv("MARGIN_FILEDROP_SFTP_USER", "tester")
    monkeypatch.setenv("MARGIN_FILEDROP_SFTP_KEY_FILE", str(server.client_key_file))
    monkeypatch.setenv("MARGIN_FILEDROP_SFTP_DIR", "/")
    monkeypatch.setenv("MARGIN_FILEDROP_SFTP_KNOWN_HOSTS", str(server.known_hosts_file))
    monkeypatch.setenv("MARGIN_FILEDROP_WORK", str(tmp_path / "work"))
    monkeypatch.setenv("MARGIN_FILEDROP_SETTLE_SECONDS", "0")
    yield server
    server.stop()


def test_sftp_end_to_end_and_read_only(sftp: InProcessSftpServer, tmp_path: Path) -> None:
    staging = tmp_path / "export"
    _write_world(staging)
    shutil.copyfile(staging / "invoice_lines.csv", sftp.root / "invoice_lines_sftp.csv")

    check = _connector().check()
    assert check.ok, check.message
    assert "sftp://tester@127.0.0.1" in check.message

    manifest = run_sync(_connector(), tmp_path / "state")
    assert manifest["records"]["invoice_lines.csv"]["rows"] == 2
    sync_dir = SyncStore(tmp_path / "state", "filedrop").latest_dir()
    assert sync_dir is not None
    assert (sync_dir / "invoice_lines.csv").read_bytes() == (
        staging / "invoice_lines.csv"
    ).read_bytes()
    lines = load_lines(sync_dir, load_mapping(MAPPING))
    assert lines.height == 2

    # Read-only toward the source: the remote file is still there, untouched.
    assert (sftp.root / "invoice_lines_sftp.csv").exists()

    # And it is not processed twice on the next sync (ledger, since the
    # remote file cannot be archived).
    manifest2 = run_sync(_connector(), tmp_path / "state")
    assert any("not processed twice" in w for w in manifest2["warnings"])


def test_sftp_permission_denied_is_a_warning(sftp: InProcessSftpServer, tmp_path: Path) -> None:
    staging = tmp_path / "export"
    _write_world(staging)
    shutil.copyfile(staging / "invoice_lines.csv", sftp.root / "invoice_lines_x.csv")
    sftp.deny_read.add("invoice_lines_x.csv")
    manifest = run_sync(_connector(), tmp_path / "state")
    assert any("could not be read" in w for w in manifest["warnings"])
    assert manifest["records"] == {}


def test_sftp_unknown_host_key_rejected(
    sftp: InProcessSftpServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = tmp_path / "wrong_known_hosts"
    wrong.write_text("", encoding="utf-8")
    monkeypatch.setenv("MARGIN_FILEDROP_SFTP_KNOWN_HOSTS", str(wrong))
    result = _connector().check()
    assert not result.ok
    assert "cannot connect" in result.message or "host" in result.message.lower()


def test_sftp_missing_settings_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARGIN_FILEDROP_DIR", raising=False)
    monkeypatch.setenv("MARGIN_FILEDROP_SFTP_HOST", "127.0.0.1")
    for env in (
        "MARGIN_FILEDROP_SFTP_USER",
        "MARGIN_FILEDROP_SFTP_KEY_FILE",
        "MARGIN_FILEDROP_SFTP_DIR",
        "MARGIN_FILEDROP_SFTP_KNOWN_HOSTS",
    ):
        monkeypatch.delenv(env, raising=False)
    result = _connector().check()
    assert not result.ok
    assert "MARGIN_FILEDROP_SFTP_USER" in result.message


def test_rules_reject_unknown_role(tmp_path: Path) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        f'rules:\n  - pattern: "x*.csv"\n    mapping: {MAPPING}\n    role: nonsense\n',
        encoding="utf-8",
    )
    with pytest.raises(ConnectorError, match="use 'lines' or 'reference:"):
        load_rules(bad)


def test_rules_reject_undeclared_reference_table(tmp_path: Path) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        f'rules:\n  - pattern: "x*.csv"\n    mapping: {MAPPING}\n    role: "reference:nope"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConnectorError, match="does not declare it"):
        load_rules(bad)


def test_materialized_frame_matches_direct_read(drop: Path, tmp_path: Path) -> None:
    """Equivalence: ingesting the connector's folder gives the same canonical
    frame as ingesting the export directly."""
    staging = tmp_path / "export"
    _write_world(staging)
    shutil.copyfile(staging / "invoice_lines.csv", drop / "invoice_lines_eq.csv")
    run_sync(_connector(), tmp_path / "state")
    sync_dir = SyncStore(tmp_path / "state", "filedrop").latest_dir()
    assert sync_dir is not None
    mapping = load_mapping(MAPPING)
    via_connector = load_lines(sync_dir, mapping)
    direct = load_lines(staging, mapping)
    assert via_connector.equals(direct)


def test_any_declared_reference_table_can_be_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 7, Phase 90's canonical-output list includes freight and receipts,
    not just lines and customers. Nothing in the connector is special-cased
    per table: a rule may name ANY reference table its mapping declares, and
    the engine's own reference loader reads the result."""
    watch = tmp_path / "drop2"
    watch.mkdir()
    monkeypatch.setenv("MARGIN_FILEDROP_DIR", str(watch))
    monkeypatch.setenv("MARGIN_FILEDROP_WORK", str(tmp_path / "work2"))
    monkeypatch.setenv("MARGIN_FILEDROP_SETTLE_SECONDS", "0")
    rules = tmp_path / "freight_rules.yaml"
    rules.write_text(
        f'rules:\n  - pattern: "freight*.csv"\n    mapping: {MAPPING}\n'
        f'    role: "reference:freight_log"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGIN_FILEDROP_RULES", str(rules))

    mapping = load_mapping(MAPPING)
    spec = mapping.reference_tables["freight_log"]
    header = ",".join(spec.columns.values())
    (watch / "freight_20260101.csv").write_text(f"{header}\nORD1,25.00\n", encoding="utf-8")

    manifest = run_sync(_connector(), tmp_path / "state2")
    assert manifest["records"]["freight_log.csv"]["rows"] == 1
    sync_dir = SyncStore(tmp_path / "state2", "filedrop").latest_dir()
    assert sync_dir is not None
    freight = load_reference_table(sync_dir, mapping, "freight_log")
    assert freight is not None and freight.height == 1
