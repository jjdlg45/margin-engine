"""System-connector registry (Run 7, Phase 90).

One place that knows how to build each connector from its settings, so the
CLI, the scheduler and the Connections screen all agree on what exists.
Connectors register here as their phases land (file drop in Phase 91,
QuickBooks Online in Phase 92, HubSpot in Phase 93).
"""

from __future__ import annotations

from collections.abc import Callable

from margin_engine.connectors.base import ConnectorError
from margin_engine.connectors.credentials import CredentialStore
from margin_engine.connectors.framework import SystemConnector

ConnectorBuilder = Callable[[CredentialStore], SystemConnector]

_BUILDERS: dict[str, ConnectorBuilder] = {}


def register(name: str, builder: ConnectorBuilder) -> None:
    _BUILDERS[name] = builder


def connector_names() -> list[str]:
    _load_builtin()
    return sorted(_BUILDERS)


def build_connector(name: str, credentials: CredentialStore | None = None) -> SystemConnector:
    _load_builtin()
    builder = _BUILDERS.get(name)
    if builder is None:
        known = ", ".join(sorted(_BUILDERS)) or "none yet"
        msg = f"unknown connector '{name}'; available connectors: {known}"
        raise ConnectorError(msg)
    return builder(credentials if credentials is not None else CredentialStore())


_loaded = False


def _load_builtin() -> None:
    """Import the built-in connector modules so they register themselves."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    import importlib

    for module in (
        "margin_engine.connectors.filedrop",
        "margin_engine.connectors.quickbooks",
        "margin_engine.connectors.hubspot",
    ):
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            # Modules land phase by phase in Run 7; one that does not exist
            # yet is simply not registered. A broken import inside an
            # existing module still raises.
            if exc.name != module:
                raise
