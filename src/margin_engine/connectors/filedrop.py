"""File-drop connector (Run 7, Phase 91).

How mid-size companies actually connect Infor CloudSuite, SAP, Epicor,
NetSuite, or a TMS to outside tools: the source system exports files on a
schedule to an agreed location, and the tool picks them up. Sources: a
local or network watched folder, and SFTP with key-based authentication.

Pick-up rules:
- only complete files are picked up (a `<name>.done` marker, or a size that
  is stable across a settle interval);
- every accepted file's checksum is recorded; a file with an
  already-processed checksum is never processed twice;
- accepted files are archived (watched folder: moved to `archive/`; SFTP:
  the remote file is never touched - the engine is read-only toward the
  source - and the downloaded copy is archived locally);
- files that match no rule or fail validation are quarantined with a
  plain-English reason in `<name>.reason.txt`;
- each file is matched to a mapping by filename pattern and validated
  against that mapping before it is accepted.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from stat import S_ISREG
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict

from margin_engine.connectors.base import ConnectorError
from margin_engine.connectors.credentials import CredentialStore
from margin_engine.connectors.framework import (
    CheckResult,
    ConnectorInfo,
    RawFile,
    SettingSpec,
    SyncBatch,
    SystemConnector,
)
from margin_engine.connectors.registry import register
from margin_engine.ingest.loader import read_table
from margin_engine.ingest.mapping import (
    MappingError,
    SourceMapping,
    TransactionTypeDerived,
    load_mapping,
)

if TYPE_CHECKING:
    import paramiko

CONNECTOR_VERSION = "1.0"

SETTINGS = (
    SettingSpec(
        env="MARGIN_FILEDROP_DIR",
        purpose="watched folder the source system exports into (local or network path)",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_RULES",
        purpose="pick-up rules YAML (filename pattern -> mapping and role); "
        "default config/filedrop.yaml",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_WORK",
        purpose="local work folder for the processed ledger, downloads, archive "
        "and quarantine; default .api_state/filedrop",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_SETTLE_SECONDS",
        purpose="seconds a file's size must stay unchanged to count as complete "
        "when no .done marker exists (default 2)",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_SFTP_HOST",
        purpose="SFTP host (set together with the other SFTP settings to pick up "
        "from SFTP instead of a watched folder)",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_SFTP_PORT",
        purpose="SFTP port (default 22)",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_SFTP_USER", purpose="SFTP user name", secret=False, optional=True
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_SFTP_KEY_FILE",
        purpose="path to the private key for key-based SFTP authentication "
        "(password auth is not supported)",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_SFTP_DIR",
        purpose="remote folder to pick up from",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="MARGIN_FILEDROP_SFTP_KNOWN_HOSTS",
        purpose="known_hosts file holding the server's host key; unknown host "
        "keys are rejected, never accepted silently",
        secret=False,
        optional=True,
    ),
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PickupRule(_StrictModel):
    """One filename pattern -> mapping and role."""

    pattern: str
    mapping: str
    # "lines" or "reference:<table>" (a table the mapping declares).
    role: str


class PickupRules(_StrictModel):
    rules: list[PickupRule]


@dataclass(frozen=True)
class _Target:
    """Where an accepted file lands: the filename its mapping expects."""

    rule: PickupRule
    mapping: SourceMapping
    served_name: str
    required_columns: list[str]
    sheet: str | int | None
    is_lines: bool


def load_rules(path: Path) -> list[_Target]:
    """Load and cross-check the pick-up rules against their mappings."""
    if not path.exists():
        msg = (
            f"file-drop rules file not found: {path}. Create it with a `rules:` "
            f"list of {{pattern, mapping, role}} entries (see docs/connectors.md)."
        )
        raise ConnectorError(msg)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        rules = PickupRules.model_validate(raw)
    except ConnectorError:
        raise
    except Exception as exc:
        msg = f"file-drop rules file {path} is invalid: {exc}"
        raise ConnectorError(msg) from exc
    targets: list[_Target] = []
    for rule in rules.rules:
        try:
            mapping = load_mapping(rule.mapping)
        except MappingError as exc:
            msg = f"file-drop rule '{rule.pattern}': its mapping failed to load: {exc}"
            raise ConnectorError(msg) from exc
        if rule.role == "lines":
            required = _lines_required_columns(mapping)
            targets.append(
                _Target(
                    rule=rule,
                    mapping=mapping,
                    served_name=mapping.lines_file,
                    required_columns=required,
                    sheet=mapping.lines_sheet,
                    is_lines=True,
                )
            )
        elif rule.role.startswith("reference:"):
            table = rule.role.removeprefix("reference:")
            ref = mapping.reference_tables.get(table)
            if ref is None:
                msg = (
                    f"file-drop rule '{rule.pattern}' names reference table "
                    f"'{table}', but mapping '{rule.mapping}' does not declare it"
                )
                raise ConnectorError(msg)
            targets.append(
                _Target(
                    rule=rule,
                    mapping=mapping,
                    served_name=ref.file,
                    required_columns=sorted(set(ref.columns.values())),
                    sheet=ref.sheet,
                    is_lines=False,
                )
            )
        else:
            msg = (
                f"file-drop rule '{rule.pattern}' has role '{rule.role}'; "
                f"use 'lines' or 'reference:<table>'"
            )
            raise ConnectorError(msg)
    return targets


def _lines_required_columns(mapping: SourceMapping) -> list[str]:
    """The source columns the engine cannot run without."""
    lines = mapping.lines
    cols = [
        lines.source_line_key.column,
        lines.invoice_id.column,
        lines.invoice_date.column,
        lines.customer_id.column,
        lines.item_id.column,
        lines.quantity.column,
    ]
    if lines.net_amount is not None:
        cols.append(lines.net_amount.column)
    if lines.unit_price is not None:
        cols.append(lines.unit_price.column)
    if isinstance(lines.transaction_type, TransactionTypeDerived):
        cols.append(lines.transaction_type.derive_from_column)
    elif lines.transaction_type is not None:
        cols.append(lines.transaction_type.column)
    return sorted(set(cols))


# -- sources -------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    name: str
    size: int
    mtime: float


class _WatchedFolder:
    """A local or network drop folder. Accepted files move to `archive/`,
    rejected ones to `quarantine/` - subfolders of the drop folder itself,
    which is the hand-off location, not the source system."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def label(self) -> str:
        return f"watched folder {self.folder}"

    def list_entries(self) -> list[_Entry]:
        out = []
        for p in sorted(self.folder.iterdir()):
            if p.is_file() and not p.name.endswith(".done") and not p.name.startswith("."):
                st = p.stat()
                out.append(_Entry(name=p.name, size=st.st_size, mtime=st.st_mtime))
        return out

    def has_done_marker(self, name: str) -> bool:
        return (self.folder / f"{name}.done").exists()

    def stat_size(self, name: str) -> int:
        return (self.folder / name).stat().st_size

    def fetch(self, name: str, dest: Path) -> None:
        shutil.copyfile(self.folder / name, dest)

    def archive_source(self, name: str, stamp: str) -> None:
        archive = self.folder / "archive"
        archive.mkdir(exist_ok=True)
        os.replace(self.folder / name, archive / f"{stamp}-{name}")
        marker = self.folder / f"{name}.done"
        if marker.exists():
            marker.unlink()

    def quarantine_source(self, name: str, stamp: str, reason: str) -> None:
        quarantine = self.folder / "quarantine"
        quarantine.mkdir(exist_ok=True)
        os.replace(self.folder / name, quarantine / f"{stamp}-{name}")
        (quarantine / f"{stamp}-{name}.reason.txt").write_text(reason + "\n", encoding="utf-8")
        marker = self.folder / f"{name}.done"
        if marker.exists():
            marker.unlink()

    def close(self) -> None:
        return None


class _SftpFolder:
    """A remote SFTP pick-up folder, strictly read-only: the connector never
    renames, moves or deletes anything on the server. Deduplication relies
    on the processed ledger instead of moving files."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        key_file: Path,
        remote_dir: str,
        known_hosts: Path,
    ) -> None:
        import paramiko

        self.remote_dir = remote_dir
        self._client = paramiko.SSHClient()
        if not known_hosts.exists():
            msg = (
                f"SFTP known_hosts file not found: {known_hosts}. Record the "
                f"server's host key there (ssh-keyscan {host} >> {known_hosts}) "
                f"and review it; unknown host keys are rejected, never accepted."
            )
            raise ConnectorError(msg)
        self._client.load_host_keys(str(known_hosts))
        self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            self._client.connect(
                host,
                port=port,
                username=user,
                key_filename=str(key_file),
                look_for_keys=False,
                allow_agent=False,
                timeout=20,
            )
        except Exception as exc:
            msg = (
                f"cannot connect to SFTP {user}@{host}:{port}: {exc}. Check the "
                f"host, port, user, private key file and known_hosts entry."
            )
            raise ConnectorError(msg) from exc
        self._sftp: paramiko.SFTPClient = self._client.open_sftp()
        self._label = f"sftp://{user}@{host}:{port}{remote_dir}"

    def label(self) -> str:
        return self._label

    def _remote(self, name: str) -> str:
        return str(PurePosixPath(self.remote_dir) / name)

    def list_entries(self) -> list[_Entry]:
        out = []
        for attr in sorted(self._sftp.listdir_attr(self.remote_dir), key=lambda a: a.filename):
            name = attr.filename
            if name.endswith(".done") or name.startswith("."):
                continue
            if attr.st_mode is not None and not S_ISREG(attr.st_mode):
                continue
            out.append(_Entry(name=name, size=attr.st_size or 0, mtime=attr.st_mtime or 0.0))
        return out

    def has_done_marker(self, name: str) -> bool:
        try:
            self._sftp.stat(self._remote(f"{name}.done"))
        except OSError:
            return False
        return True

    def stat_size(self, name: str) -> int:
        size = self._sftp.stat(self._remote(name)).st_size
        return size or 0

    def fetch(self, name: str, dest: Path) -> None:
        self._sftp.get(self._remote(name), str(dest))

    def archive_source(self, name: str, stamp: str) -> None:
        return None  # read-only toward the server; the local copy is archived

    def quarantine_source(self, name: str, stamp: str, reason: str) -> None:
        return None  # read-only toward the server; recorded in the ledger

    def close(self) -> None:
        self._sftp.close()
        self._client.close()


# -- the connector -------------------------------------------------------------


class FileDropConnector(SystemConnector):
    def __init__(self, credentials: CredentialStore) -> None:
        self._creds = credentials

    # settings ------------------------------------------------------------

    def _rules_path(self) -> Path:
        return Path(self._creds.get("MARGIN_FILEDROP_RULES") or "config/filedrop.yaml")

    def _work_dir(self) -> Path:
        return Path(self._creds.get("MARGIN_FILEDROP_WORK") or ".api_state/filedrop")

    def _settle_seconds(self) -> float:
        raw = self._creds.get("MARGIN_FILEDROP_SETTLE_SECONDS") or "2"
        try:
            return max(float(raw), 0.0)
        except ValueError:
            return 2.0

    def _open_source(self) -> _WatchedFolder | _SftpFolder:
        host = self._creds.get("MARGIN_FILEDROP_SFTP_HOST")
        if host:
            missing = [
                env
                for env in (
                    "MARGIN_FILEDROP_SFTP_USER",
                    "MARGIN_FILEDROP_SFTP_KEY_FILE",
                    "MARGIN_FILEDROP_SFTP_DIR",
                    "MARGIN_FILEDROP_SFTP_KNOWN_HOSTS",
                )
                if not self._creds.get(env)
            ]
            if missing:
                msg = (
                    f"SFTP pick-up is configured (MARGIN_FILEDROP_SFTP_HOST is set) "
                    f"but these settings are missing: {', '.join(missing)}"
                )
                raise ConnectorError(msg)
            port_raw = self._creds.get("MARGIN_FILEDROP_SFTP_PORT") or "22"
            try:
                port = int(port_raw)
            except ValueError:
                msg = f"MARGIN_FILEDROP_SFTP_PORT must be a number, got '{port_raw}'"
                raise ConnectorError(msg) from None
            return _SftpFolder(
                host=host,
                port=port,
                user=self._creds.get("MARGIN_FILEDROP_SFTP_USER") or "",
                key_file=Path(self._creds.get("MARGIN_FILEDROP_SFTP_KEY_FILE") or ""),
                remote_dir=self._creds.get("MARGIN_FILEDROP_SFTP_DIR") or "",
                known_hosts=Path(self._creds.get("MARGIN_FILEDROP_SFTP_KNOWN_HOSTS") or ""),
            )
        folder_raw = self._creds.get("MARGIN_FILEDROP_DIR")
        if not folder_raw:
            msg = (
                "file drop needs MARGIN_FILEDROP_DIR (a watched folder) or the "
                "MARGIN_FILEDROP_SFTP_* settings (an SFTP pick-up)"
            )
            raise ConnectorError(msg)
        folder = Path(folder_raw)
        if not folder.is_dir():
            msg = f"watched folder does not exist or is not a folder: {folder}"
            raise ConnectorError(msg)
        return _WatchedFolder(folder)

    # ledger ----------------------------------------------------------------

    def _ledger_path(self) -> Path:
        return self._work_dir() / "processed.json"

    def _read_ledger(self) -> list[dict[str, object]]:
        p = self._ledger_path()
        if not p.exists():
            return []
        entries = json.loads(p.read_text(encoding="utf-8"))
        return list(entries)

    def _write_ledger(self, entries: list[dict[str, object]]) -> None:
        p = self._ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    # interface -------------------------------------------------------------

    def describe(self) -> ConnectorInfo:
        produces: tuple[str, ...]
        try:
            produces = tuple(sorted({t.served_name for t in load_rules(self._rules_path())}))
        except ConnectorError:
            produces = ()
        return ConnectorInfo(
            name="filedrop",
            version=CONNECTOR_VERSION,
            system="File drop (watched folder / SFTP)",
            description=(
                "Picks up scheduled exports from Infor, SAP, Epicor, NetSuite, a "
                "TMS or any system that can write files to a folder or SFTP site; "
                "each file is validated against its mapping before it is accepted."
            ),
            produces=produces,
            required_settings=SETTINGS,
            scopes=("read-only file access",),
            live_verification=(
                "verified end to end against a local watched folder and an "
                "in-process SFTP server (2026-11-02)"
            ),
        )

    def check(self) -> CheckResult:
        try:
            targets = load_rules(self._rules_path())
        except ConnectorError as exc:
            return CheckResult(ok=False, message=str(exc))
        try:
            source = self._open_source()
        except ConnectorError as exc:
            return CheckResult(ok=False, message=str(exc))
        try:
            n = len(source.list_entries())
        except Exception as exc:
            source.close()
            return CheckResult(ok=False, message=f"{source.label()}: cannot list files: {exc}")
        label = source.label()
        source.close()
        return CheckResult(
            ok=True,
            message=(
                f"{label} is reachable with {n} file(s) waiting; "
                f"{len(targets)} pick-up rule(s) loaded."
            ),
        )

    def sync(self, since: str | None) -> SyncBatch:
        targets = load_rules(self._rules_path())
        work = self._work_dir()
        incoming = work / "incoming"
        archive = work / "archive"
        quarantine = work / "quarantine"
        for d in (incoming, archive, quarantine):
            d.mkdir(parents=True, exist_ok=True)
        ledger = self._read_ledger()
        seen_checksums = {str(e["sha256"]) for e in ledger}
        warnings: list[str] = []
        raw_files: dict[str, RawFile] = {}
        now = datetime.now(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%S")

        source = self._open_source()
        try:
            entries = source.list_entries()
            settle = self._settle_seconds()
            complete: list[_Entry] = []
            for e in entries:
                if source.has_done_marker(e.name):
                    complete.append(e)
                    continue
                if settle > 0:
                    time.sleep(settle)
                if source.stat_size(e.name) == e.size:
                    complete.append(e)
                else:
                    warnings.append(
                        f"{e.name}: still being written (size changed within "
                        f"{settle:.0f}s); it will be picked up next sync"
                    )

            for e in complete:
                local = incoming / f"{stamp}-{e.name}"
                try:
                    source.fetch(e.name, local)
                except Exception as exc:
                    warnings.append(f"{e.name}: could not be read ({exc}); left in place")
                    continue
                digest = hashlib.sha256(local.read_bytes()).hexdigest()
                if digest in seen_checksums:
                    warnings.append(
                        f"{e.name}: identical to an already-processed file "
                        f"(checksum match); not processed twice"
                    )
                    source.archive_source(e.name, stamp)
                    dup_dest = archive / f"{stamp}-duplicate-{e.name}"
                    os.replace(local, dup_dest)
                    ledger.append(
                        {
                            "file": e.name,
                            "sha256": digest,
                            "status": "duplicate",
                            "when_utc": now.isoformat(timespec="seconds"),
                        }
                    )
                    continue
                target = next((t for t in targets if fnmatch.fnmatch(e.name, t.rule.pattern)), None)
                if target is None:
                    patterns = ", ".join(t.rule.pattern for t in targets)
                    reason = (
                        f"No pick-up rule matches '{e.name}'. Known patterns: "
                        f"{patterns}. Add a rule to {self._rules_path()} or rename "
                        f"the export."
                    )
                    self._quarantine(source, e.name, local, quarantine, stamp, reason)
                    ledger.append(
                        {
                            "file": e.name,
                            "sha256": digest,
                            "status": "quarantined",
                            "reason": reason,
                            "when_utc": now.isoformat(timespec="seconds"),
                        }
                    )
                    warnings.append(f"{e.name}: quarantined - {reason}")
                    continue
                ok, rows, reason = self._validate(local, target)
                if not ok:
                    self._quarantine(source, e.name, local, quarantine, stamp, reason)
                    ledger.append(
                        {
                            "file": e.name,
                            "sha256": digest,
                            "status": "quarantined",
                            "reason": reason,
                            "when_utc": now.isoformat(timespec="seconds"),
                        }
                    )
                    warnings.append(f"{e.name}: quarantined - {reason}")
                    continue
                # Accepted.
                source.archive_source(e.name, stamp)
                final_local = archive / f"{stamp}-{e.name}"
                os.replace(local, final_local)
                if target.served_name in raw_files:
                    warnings.append(
                        f"{e.name}: supersedes an earlier file for "
                        f"'{target.served_name}' accepted in the same sync"
                    )
                raw_files[target.served_name] = RawFile(
                    name=target.served_name, source=final_local, rows=rows
                )
                ledger.append(
                    {
                        "file": e.name,
                        "sha256": digest,
                        "status": "processed",
                        "rows": rows,
                        "served_as": target.served_name,
                        "mapping": target.rule.mapping,
                        "when_utc": now.isoformat(timespec="seconds"),
                    }
                )
        finally:
            source.close()

        self._write_ledger(ledger)
        cursor = now.isoformat(timespec="seconds")
        return SyncBatch(
            tables={},
            cursor=cursor,
            window_start=since,
            window_end=cursor,
            warnings=warnings,
            raw_files=sorted(raw_files.values(), key=lambda rf: rf.name),
        )

    @staticmethod
    def _quarantine(
        source: _WatchedFolder | _SftpFolder,
        name: str,
        local: Path,
        quarantine: Path,
        stamp: str,
        reason: str,
    ) -> None:
        source.quarantine_source(name, stamp, reason)
        dest = quarantine / f"{stamp}-{name}"
        os.replace(local, dest)
        (quarantine / f"{stamp}-{name}.reason.txt").write_text(reason + "\n", encoding="utf-8")

    @staticmethod
    def _validate(path: Path, target: _Target) -> tuple[bool, int, str]:
        """Parse the file under its mapping's input options and require the
        columns the mapping names. Returns (ok, rows, reason)."""
        override = target.mapping.lines_input if target.is_lines else None
        if not target.is_lines:
            table_name = target.rule.role.removeprefix("reference:")
            ref = target.mapping.reference_tables[table_name]
            override = ref.input
        opts, _sep = target.mapping.effective_input(override)
        try:
            frame, _blanks = read_table(path, opts, sheet=target.sheet)
        except Exception as exc:
            return (
                False,
                0,
                f"The file could not be parsed under mapping '{target.rule.mapping}': {exc}",
            )
        missing = [c for c in target.required_columns if c not in frame.columns]
        if missing:
            return (
                False,
                0,
                (
                    f"The file is missing column(s) {missing} that mapping "
                    f"'{target.rule.mapping}' requires for role '{target.rule.role}'. "
                    f"Columns found: {frame.columns}."
                ),
            )
        if frame.height == 0:
            return False, 0, "The file has headers but no data rows."
        return True, frame.height, ""


register("filedrop", lambda creds: FileDropConnector(creds))
