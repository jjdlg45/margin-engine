"""In-process SFTP test server (Run 7, Phase 91) - no Docker needed.

A real paramiko SFTP server speaking the real protocol over a loopback
socket, serving one local folder read-only, with public-key authentication.
Used by tests/test_filedrop.py to exercise the SFTP pick-up end to end.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from pathlib import Path
from typing import Any

import paramiko
from paramiko import (
    AUTH_FAILED,
    AUTH_SUCCESSFUL,
    OPEN_SUCCEEDED,
    RSAKey,
    ServerInterface,
    SFTPAttributes,
    SFTPHandle,
    SFTPServer,
    SFTPServerInterface,
)
from paramiko.sftp import SFTP_NO_SUCH_FILE, SFTP_PERMISSION_DENIED


class _AuthServer(ServerInterface):
    def __init__(self, allowed_key: paramiko.PKey) -> None:
        self._allowed = allowed_key

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        if key.get_fingerprint() == self._allowed.get_fingerprint():
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "publickey"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _ReadOnlySftp(SFTPServerInterface):
    """Serves `root` read-only. Writes are refused, which also proves the
    connector never tries to write to the source system."""

    root: Path = Path(".")
    deny_read: frozenset[str] = frozenset()

    def _real(self, path: str) -> Path:
        clean = path.replace("\\", "/").lstrip("/")
        return (self.root / clean).resolve()

    def list_folder(self, path: str) -> list[SFTPAttributes] | int:
        real = self._real(path)
        if not real.is_dir():
            return SFTP_NO_SUCH_FILE
        out = []
        for p in sorted(real.iterdir()):
            attr = SFTPAttributes.from_stat(p.stat())
            attr.filename = p.name
            out.append(attr)
        return out

    def stat(self, path: str) -> SFTPAttributes | int:
        real = self._real(path)
        if not real.exists():
            return SFTP_NO_SUCH_FILE
        return SFTPAttributes.from_stat(real.stat())

    lstat = stat

    def open(self, path: str, flags: int, attr: SFTPAttributes) -> SFTPHandle | int:
        import os

        real = self._real(path)
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
            return SFTP_PERMISSION_DENIED
        if real.name in self.deny_read:
            return SFTP_PERMISSION_DENIED
        if not real.is_file():
            return SFTP_NO_SUCH_FILE
        handle = SFTPHandle(flags)
        handle.readfile = real.open("rb")  # type: ignore[attr-defined]
        return handle

    # Every mutation is refused: the server is read-only.
    def remove(self, path: str) -> int:
        return SFTP_PERMISSION_DENIED

    def rename(self, oldpath: str, newpath: str) -> int:
        return SFTP_PERMISSION_DENIED

    def mkdir(self, path: str, attr: SFTPAttributes) -> int:
        return SFTP_PERMISSION_DENIED

    def rmdir(self, path: str) -> int:
        return SFTP_PERMISSION_DENIED

    def chattr(self, path: str, attr: SFTPAttributes) -> int:
        return SFTP_PERMISSION_DENIED


class InProcessSftpServer:
    """Start with `start()`, stop with `stop()`. Exposes everything the
    connector needs: port, a client private-key file and a known_hosts file."""

    def __init__(self, root: Path, work_dir: Path) -> None:
        self.root = root
        self.host_key = RSAKey.generate(2048)
        self.client_key = RSAKey.generate(2048)
        self.client_key_file = work_dir / "client_key"
        self.client_key.write_private_key_file(str(self.client_key_file))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port: int = self._sock.getsockname()[1]
        self.known_hosts_file = work_dir / "known_hosts"
        self.known_hosts_file.write_text(
            f"[127.0.0.1]:{self.port} {self.host_key.get_name()} {self.host_key.get_base64()}\n",
            encoding="utf-8",
        )
        self._transports: list[paramiko.Transport] = []
        self._accept_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.deny_read: set[str] = set()

    def start(self) -> None:
        self._sock.listen(8)

        server = self

        class _Sftp(_ReadOnlySftp):
            def __init__(self, srv: SFTPServer, *args: Any, **kwargs: Any) -> None:
                super().__init__(srv, *args, **kwargs)
                self.root = server.root
                self.deny_read = frozenset(server.deny_read)

        def accept_loop() -> None:
            while not self._stopping.is_set():
                try:
                    conn, _addr = self._sock.accept()
                except OSError:
                    return
                transport = paramiko.Transport(conn)
                transport.add_server_key(self.host_key)
                transport.set_subsystem_handler("sftp", SFTPServer, _Sftp)
                try:
                    transport.start_server(server=_AuthServer(self.client_key))
                except Exception:
                    transport.close()
                    continue
                self._transports.append(transport)

        self._accept_thread = threading.Thread(target=accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        self._stopping.set()
        with contextlib.suppress(OSError):
            self._sock.close()
        for t in self._transports:
            t.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5)
