"""Connector credential store (Run 7, Phase 90).

Default: settings come from environment variables or a local `.env` file
(gitignored). Production mode adds an encrypted-at-rest keystore (Fernet,
key supplied via an environment variable) so server-side connector
credentials never sit on disk in plain text.

Rules: a credential never appears in the repo, logs, exceptions, test
output, or any manifest. This module therefore never puts a credential
value inside an exception message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from margin_engine.connectors.base import ConnectorError

if TYPE_CHECKING:
    from cryptography.fernet import Fernet
from margin_engine.connectors.framework import ConnectorInfo, SettingSpec

KEYSTORE_KEY_ENV = "MARGIN_CONNECTOR_KEY"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; `#` comments, blank lines and an optional
    `export ` prefix are tolerated; single or double quotes are stripped."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


class EncryptedKeyStore:
    """A Fernet-encrypted JSON file of connector settings (production mode).

    The encryption key comes from the MARGIN_CONNECTOR_KEY environment
    variable (a Fernet key: 32 url-safe base64 bytes); the key itself is
    never written anywhere by this class.
    """

    def __init__(self, path: Path, *, key_env: str = KEYSTORE_KEY_ENV) -> None:
        self.path = path
        self.key_env = key_env

    def _fernet(self) -> Fernet:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - dependency present in api group
            msg = (
                "the encrypted connector keystore needs the 'cryptography' package "
                "(installed with the api dependency group: uv sync --group api)"
            )
            raise ConnectorError(msg) from exc
        key = os.environ.get(self.key_env)
        if not key:
            msg = (
                f"the encrypted connector keystore requires the {self.key_env} "
                f'environment variable (generate one with: python -c "from '
                f'cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")'
            )
            raise ConnectorError(msg)
        return Fernet(key.encode("ascii"))

    def _read_all(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        fernet = self._fernet()
        from cryptography.fernet import InvalidToken

        try:
            decrypted = fernet.decrypt(self.path.read_bytes())
        except InvalidToken as exc:
            msg = (
                f"the connector keystore at {self.path} cannot be decrypted with the "
                f"key in {self.key_env}; the key changed or the file is corrupt"
            )
            raise ConnectorError(msg) from exc
        data = json.loads(decrypted.decode("utf-8"))
        return {str(k): str(v) for k, v in data.items()}

    def get(self, name: str) -> str | None:
        return self._read_all().get(name)

    def set(self, name: str, value: str) -> None:
        values = self._read_all() if self.path.exists() else {}
        values[name] = value
        fernet = self._fernet()
        payload = json.dumps(values).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(fernet.encrypt(payload))
        os.replace(tmp, self.path)

    def names(self) -> list[str]:
        return sorted(self._read_all())


class CredentialStore:
    """Resolution order: process environment, then `.env`, then the
    encrypted keystore (when configured)."""

    def __init__(
        self,
        *,
        env_file: Path | None = None,
        keystore: EncryptedKeyStore | None = None,
    ) -> None:
        self._env_file = env_file if env_file is not None else Path(".env")
        self._keystore = keystore
        self._file_values: dict[str, str] | None = None

    def _from_file(self, name: str) -> str | None:
        if self._file_values is None:
            self._file_values = parse_env_file(self._env_file)
        return self._file_values.get(name)

    def get(self, name: str) -> str | None:
        value = os.environ.get(name)
        if value:
            return value
        value = self._from_file(name)
        if value:
            return value
        if self._keystore is not None and self._keystore.path.exists():
            return self._keystore.get(name)
        return None

    def missing(self, info: ConnectorInfo) -> list[SettingSpec]:
        """The settings a connector still needs, for a stop-and-ask message."""
        return [s for s in info.required_settings if not s.optional and not self.get(s.env)]

    # There was a missing_message() formatter here until the Run 8 wiring
    # audit: nothing called it, because each of the four places that reports a
    # credential gap (the CLI, the connections endpoint, and the two API
    # connectors' own check()) words it for its own context. missing() above is
    # the shared part, and it is what they all use.
