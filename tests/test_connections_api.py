"""Run 7, Phase 94: the Connections endpoints.

Local mode (open), production mode (admin-only), the OpenAPI contract, and
a real file-drop sync driven through the API against temp folders.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from api.auth import MODE_PRODUCTION, hash_password
from api.main import create_app
from api.settings import Settings
from fastapi.testclient import TestClient

from tests.fixtures.builder import FixtureWorld

REPO = Path(__file__).resolve().parents[1]
SPEC = yaml.safe_load((REPO / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8"))
PASSWORD = "correct-horse-battery"


def _validate(payload: Any, schema_name: str) -> None:
    import jsonschema

    schema = dict(SPEC["components"]["schemas"][schema_name])
    schema["$defs"] = SPEC["components"]["schemas"]
    jsonschema.validate(payload, schema)


def test_routes_are_in_the_spec() -> None:
    for path in (
        "/connections",
        "/connections/{name}/check",
        "/connections/{name}/sync",
        "/connections/{name}/schedule",
    ):
        assert path in SPEC["paths"], f"{path} missing from docs/api/openapi.yaml"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        # The connections endpoints never read the bundle; point at an empty
        # dir so these tests stay independent of the exported demo data.
        "bundle_dir": tmp_path / "no-bundle",
        "db_path": tmp_path / "api.duckdb",
        "actions_db": tmp_path / "actions.duckdb",
        "audit_log": tmp_path / "audit.log",
        "uploads_dir": tmp_path / "uploads",
        "drift_dir": tmp_path / "drift",
        "connectors_dir": tmp_path / "connectors",
        "connectors_spec": "",
        "fx_refresh_s": 100_000,
        "ppi_refresh_s": 100_000,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def local_client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(tmp_path))) as client:
        yield client


@pytest.fixture
def drop_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    watch = tmp_path / "drop"
    watch.mkdir()
    monkeypatch.setenv("MARGIN_FILEDROP_DIR", str(watch))
    monkeypatch.setenv("MARGIN_FILEDROP_WORK", str(tmp_path / "fd-work"))
    monkeypatch.setenv("MARGIN_FILEDROP_SETTLE_SECONDS", "0")
    monkeypatch.delenv("MARGIN_FILEDROP_SFTP_HOST", raising=False)
    return watch


def test_list_connections_local_mode(local_client: TestClient, drop_env: Path) -> None:
    resp = local_client.get("/api/connections")
    assert resp.status_code == 200
    payload = resp.json()
    _validate(payload, "ConnectionsList")
    by_name = {c["name"]: c for c in payload["connectors"]}
    assert set(by_name) == {"filedrop", "hubspot", "quickbooks_online"}
    assert by_name["filedrop"]["configured"] is True  # all settings optional
    assert by_name["hubspot"]["configured"] in (True, False)
    assert "documented API" in by_name["quickbooks_online"]["live_verification"]
    assert by_name["filedrop"]["last_sync"] is None


def test_check_and_sync_filedrop_through_api(
    local_client: TestClient, drop_env: Path, tmp_path: Path
) -> None:
    world = FixtureWorld(tmp=tmp_path / "export")
    world.add_line(INV_NO="I1", CUST_NO="C1", ITEM_NO="A1", QTY_SHP="2", EXT_NET="20.00")
    world.write()
    shutil.copyfile(tmp_path / "export" / "invoice_lines.csv", drop_env / "invoice_lines_api.csv")

    check = local_client.post("/api/connections/filedrop/check")
    assert check.status_code == 200
    assert check.json()["ok"] is True

    resp = local_client.post("/api/connections/filedrop/sync")
    assert resp.status_code == 200
    sync = resp.json()["sync"]
    _validate(sync, "SyncSummary")
    assert sync["records"]["invoice_lines.csv"]["rows"] == 1

    # Idempotent second sync through the API.
    again = local_client.post("/api/connections/filedrop/sync")
    assert again.status_code == 200
    assert again.json()["sync"]["unchanged_rerun"] is True

    listed = local_client.get("/api/connections").json()
    filedrop = next(c for c in listed["connectors"] if c["name"] == "filedrop")
    assert filedrop["last_sync"]["records"]["invoice_lines.csv"]["rows"] == 1


def test_unknown_connector_404(local_client: TestClient) -> None:
    for method, url in (
        ("post", "/api/connections/sap_direct/check"),
        ("post", "/api/connections/sap_direct/sync"),
        ("put", "/api/connections/sap_direct/schedule"),
    ):
        resp = getattr(local_client, method)(url, json={"interval_s": 600})
        assert resp.status_code == 404
        assert "unknown connector" in resp.json()["detail"]


def test_sync_failure_is_409_with_plain_message(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MARGIN_FILEDROP_DIR", raising=False)
    monkeypatch.delenv("MARGIN_FILEDROP_SFTP_HOST", raising=False)
    monkeypatch.setenv("MARGIN_FILEDROP_WORK", str(tmp_path / "fd-work"))
    resp = local_client.post("/api/connections/filedrop/sync")
    assert resp.status_code == 409
    assert "MARGIN_FILEDROP_DIR" in resp.json()["detail"]


def test_schedule_put_validates_and_stores(local_client: TestClient, tmp_path: Path) -> None:
    bad = local_client.put("/api/connections/filedrop/schedule", json={"interval": 10})
    assert bad.status_code == 400

    ok = local_client.put("/api/connections/filedrop/schedule", json={"interval_s": 10})
    assert ok.status_code == 200
    body = ok.json()
    assert body["interval_s"] == 60.0, "clamped to the minimum"
    assert body["scheduled"] is False, "not in MARGIN_API_CONNECTORS"
    assert "not" in body["note"]
    stored = (tmp_path / "connectors" / "schedules.json").read_text(encoding="utf-8")
    assert '"filedrop"' in stored


def test_scheduled_flag_reflects_spec(tmp_path: Path) -> None:
    settings = _settings(tmp_path, connectors_spec="filedrop:86400")
    with TestClient(create_app(settings)) as client:
        listed = client.get("/api/connections").json()
        filedrop = next(c for c in listed["connectors"] if c["name"] == "filedrop")
        assert filedrop["scheduled"] is True
        assert filedrop["schedule_interval_s"] == 86400.0
        changed = client.put("/api/connections/filedrop/schedule", json={"interval_s": 7200})
        assert changed.json()["scheduled"] is True
        assert changed.json()["interval_s"] == 7200.0


# -- production mode ------------------------------------------------------------


@pytest.fixture
def prod_client(tmp_path: Path) -> Iterator[TestClient]:
    h = hash_password(PASSWORD, iterations=1000)
    settings = _settings(
        tmp_path,
        mode=MODE_PRODUCTION,
        storage_encrypted=True,
        accounts_spec=(f"u-admin|Ada Admin|admin|{h}\nu-analyst|Ben Analyst|analyst|{h}"),
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _login(client: TestClient, user: str) -> dict[str, str]:
    resp = client.post("/api/auth/login", json={"user_id": user, "password": PASSWORD})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_production_mode_requires_admin(prod_client: TestClient) -> None:
    anonymous = prod_client.get("/api/connections")
    assert anonymous.status_code == 401

    analyst = prod_client.get("/api/connections", headers=_login(prod_client, "u-analyst"))
    assert analyst.status_code == 403
    assert "admin" in analyst.json()["detail"]

    admin_headers = _login(prod_client, "u-admin")
    admin = prod_client.get("/api/connections", headers=admin_headers)
    assert admin.status_code == 200
    check = prod_client.post("/api/connections/hubspot/check", headers=admin_headers)
    assert check.status_code == 200

    # Client-supplied identity is rejected in production: the authenticator
    # answers 400 (X-User is not accepted there), never a principal.
    spoofed = prod_client.get("/api/connections", headers={"X-User": "u-admin"})
    assert spoofed.status_code == 400
