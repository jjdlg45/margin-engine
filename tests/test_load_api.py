"""Load-your-own-data endpoints (polish round): upload, profile, click-confirm
mapping, background run, and the fail-closed paths — unknown uploads, missing
confirmations, unknown paths, and running before a mapping exists."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from api.main import create_app
from api.settings import Settings
from fastapi.testclient import TestClient

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample" / "erp" / "invoice_lines.csv"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        bundle_dir=tmp_path / "nobundle",
        db_path=tmp_path / "api.duckdb",
        actions_db=tmp_path / "actions.duckdb",
        uploads_dir=tmp_path / "uploads",
        drift_dir=tmp_path / "drift",
        fx_refresh_s=10_000,
        ppi_refresh_s=10_000,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def _upload(client: TestClient) -> str:
    r = client.post(
        "/api/load/upload",
        params={"filename": "invoice_lines.csv"},
        content=SAMPLE.read_bytes(),
    )
    assert r.status_code == 200, r.text
    return str(r.json()["upload_id"])


def test_upload_rejects_bad_input(client: TestClient) -> None:
    r = client.post("/api/load/upload", params={"filename": "report.pdf"}, content=b"x")
    assert r.status_code == 422
    assert ".csv or .xlsx" in r.json()["detail"]
    r = client.post("/api/load/upload", params={"filename": "a.csv"}, content=b"")
    assert r.status_code == 422


def test_profile_suggests_and_lists_confirmables(client: TestClient) -> None:
    uid = _upload(client)
    r = client.post(f"/api/load/profile/{uid}", params={"txn_column": "TXN_CD"})
    assert r.status_code == 200
    p = r.json()
    assert p["n_rows"] > 0
    # The true transaction-code column is a candidate and, chosen, leads.
    assert p["txn_candidates"][0] == "TXN_CD"
    paths = {c["value_path"] for c in p["confirmables"]}
    # Required fields, semantics and per-code meanings are all confirmable.
    assert "lines.customer_id.column" in paths
    assert "lines.net_amount.sign_convention" in paths
    assert "lines.transaction_type.code_map.S" in paths
    # No cost candidate: the person must explicitly declare its absence.
    assert "cost_unavailable" in paths
    # Every confirmable has a plain current value and a kind the UI can render.
    for c in p["confirmables"]:
        assert c["kind"] in ("column", "choice", "text")
        if c["kind"] in ("column", "choice"):
            assert c["choices"]


def _confirmed_values(client: TestClient, uid: str) -> dict[str, str]:
    p = client.post(f"/api/load/profile/{uid}", params={"txn_column": "TXN_CD"}).json()
    values = {c["value_path"]: str(c["current"]) for c in p["confirmables"]}
    values.update(
        {
            "lines.invoice_date.column": "INV_DT",
            "lines.invoice_date.format": "%m/%d/%Y",
            "lines.net_amount.column": "EXT_NET",
            "lines.transaction_type.code_map.S": "sale",
            "lines.transaction_type.code_map.R": "return",
            "lines.transaction_type.code_map.C": "credit",
            "lines.transaction_type.code_map.F": "free_good",
        }
    )
    return {k: v for k, v in values.items() if k in {c["value_path"] for c in p["confirmables"]}}


def test_mapping_requires_every_confirmation_and_only_known_paths(client: TestClient) -> None:
    uid = _upload(client)
    values = _confirmed_values(client, uid)
    partial = dict(list(values.items())[:3])
    r = client.post(f"/api/load/mapping/{uid}", json={"values": partial, "txn_column": "TXN_CD"})
    assert r.status_code == 422
    assert "needs your confirmation" in r.json()["detail"]
    bad = dict(values)
    bad["lines.made_up.column"] = "CUST_NO"
    r = client.post(f"/api/load/mapping/{uid}", json={"values": bad, "txn_column": "TXN_CD"})
    assert r.status_code == 422
    assert "not confirmable" in r.json()["detail"]


def test_mapping_validation_error_is_plain_english(client: TestClient) -> None:
    uid = _upload(client)
    values = _confirmed_values(client, uid)
    values["lines.invoice_date.column"] = "FIXME_UNKNOWN"
    r = client.post(f"/api/load/mapping/{uid}", json={"values": values, "txn_column": "TXN_CD"})
    # The saved mapping loads (schema-valid), so the refusal arrives at run
    # time from the loader; or the schema refuses here - either way the
    # message is actionable, never a stack trace.
    if r.status_code == 200:
        run = client.post(f"/api/load/run/{uid}")
        assert run.status_code == 200
        for _ in range(600):
            s = client.get(f"/api/load/status/{uid}").json()
            if s["state"] != "running":
                break
            time.sleep(0.5)
        assert s["state"] == "failed"
        assert "FIXME_UNKNOWN" in s["message"]
        assert "Traceback" not in s["message"]
    else:
        assert r.status_code == 422


def test_run_refuses_without_mapping(client: TestClient) -> None:
    uid = _upload(client)
    r = client.post(f"/api/load/run/{uid}")
    assert r.status_code == 409
    assert "Confirm the mapping first" in r.json()["detail"]


def test_full_flow_runs_and_serves_the_new_bundle(client: TestClient) -> None:
    uid = _upload(client)
    values = _confirmed_values(client, uid)
    r = client.post(f"/api/load/mapping/{uid}", json={"values": values, "txn_column": "TXN_CD"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/load/run/{uid}")
    assert r.status_code == 200
    for _ in range(900):
        s = client.get(f"/api/load/status/{uid}").json()
        if s["state"] != "running":
            break
        time.sleep(1)
    assert s["state"] == "done", s
    app = client.get("/api/app").json()
    assert app["data_label"] == "Your data: invoice_lines.csv (local run)"
    assert client.get("/api/pages/overview").status_code == 200
    assert client.get("/api/pages/actions").status_code == 200
