"""Append-only access and audit log (Run 6, Phase 82, Finding 19).

One JSON object per line: when, who (the immutable user id), what was
attempted, and how it came out. Enough to answer "who approved this and
when" months later, and deliberately not enough to leak anything.

What never reaches this file: request bodies, passwords, tokens,
Authorization headers, uploaded content, query strings. The redaction is a
positive list - fields are copied in by name rather than filtered out - so a
new field somewhere else in the API cannot start appearing here by accident.
A test reads the log back and fails on any secret it can find.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

#: Field names whose VALUES must never be written, checked on the way in as a
#: second line behind the positive list.
FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "token",
        "authorization",
        "secret",
        "salt",
        "api_key",
        "cookie",
        "body",
        "content",
    }
)


class AuditLog:
    """A JSON-lines file opened in append mode, one line flushed per event."""

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Created before first use so a reader never has to guess whether
            # an empty log means "nothing happened" or "never started".
            if not path.exists():
                path.touch()
                _restrict(path)

    def record(
        self,
        *,
        action: str,
        user_id: str | None,
        status: int,
        resource: str | None = None,
        detail: str | None = None,
        **extra: Any,
    ) -> None:
        if not self.enabled:
            return
        bad = sorted(k for k in extra if k.lower() in FORBIDDEN_KEYS)
        if bad:
            msg = f"refusing to write {bad} to the audit log"
            raise ValueError(msg)
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": action,
            "user_id": user_id or "anonymous",
            "status": int(status),
            "resource": resource,
            "detail": detail,
            **extra,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out


def _restrict(path: Path) -> None:
    """Owner-only where the platform supports it.

    On POSIX this is 0600. On Windows the equivalent is an ACL change that
    needs pywin32, so the mode call is a no-op there and
    docs/deploy-api.md says plainly that the state directory's protection is
    the operator's to arrange.
    """
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, 0o600)
