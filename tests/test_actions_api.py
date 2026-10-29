"""Phase 39: maker-checker API.

Self-approval blocked; justification enforced on compliance-flagged approvals;
the event log is complete and valid after a lifecycle driven over HTTP; bulk
approve applies per-item guardrails; edits are audited and guardrail-checked;
manual proposals respect the floor; responses match the OpenAPI contract.
Identity is a configured user name (X-User) - local mode, not authentication.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml
from api.main import create_app
from api.settings import Settings
from fastapi.testclient import TestClient

from margin_engine.actions.generate import ACTIONS_SCHEMA
from margin_engine.actions.model import EVENTS_SCHEMA, replay_status, validate_log
from margin_engine.actions.store import ActionStore

REPO = Path(__file__).resolve().parents[1]
SPEC = yaml.safe_load((REPO / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8"))

AT = dt.datetime(2026, 1, 26, tzinfo=dt.UTC)


def _mk_action(
    aid: str,
    *,
    flags: str = "",
    blocked: bool = False,
    floor: int | None = 1_000,
) -> dict[str, Any]:
    return {
        "action_id": aid,
        "action_type": "price_change",
        "title": f"Raise price {aid}",
        "rationale": "test",
        "customer_id": "C1",
        "item_id": "I1",
        "scope": "customer_item",
        "uom": "EA",
        "segment_leaf": 1,
        "guidance_status": "below_reference",
        "evidence_status": "descriptive_elasticity",
        "volume_treatment": "fixed_volume_only",
        "predicted_volume_ratio": 1.0,
        "impact_basis": "gross_price_opportunity_fixed_volume",
        "expected_impact_cents": 10_000,
        "current_price_cents": 900,
        "proposed_price_cents": 1_200,
        "floor_price_cents": floor,
        "pass_through_level": None,
        "incremental_cost_cents": None,
        "n_ship_tos": 1,
        "scope_key": None,
        "baseline_period": None,
        "intervention": None,
        "affected_set": None,
        "scenario_id": None,
        "overlap_group": None,
        "input_fingerprint": None,
        "config_fingerprint": None,
        "model_fingerprint": None,
        "expires_on": None,
        "priority": "medium",
        "owner": "unassigned",
        "due_date": dt.date(2026, 2, 25),
        "compliance_flags": flags,
        "blocked": blocked,
        "blocked_reason": "contract-protected line" if blocked else None,
        "created_at": AT,
    }


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    actions = pl.DataFrame(
        [
            _mk_action("act_aaa000000001"),
            _mk_action("act_bbb000000002", blocked=True),
            _mk_action("act_ccc000000003", flags="robinson_patman"),
            _mk_action("act_ddd000000004"),
        ],
    ).cast(ACTIONS_SCHEMA)
    events = pl.DataFrame(
        [
            {
                "action_id": aid,
                "seq": 0,
                "who": "engine",
                "at": AT,
                "from_status": None,
                "to_status": "proposed",
                "note": "generated",
            }
            for aid in actions["action_id"].to_list()
        ],
        schema=EVENTS_SCHEMA,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    actions.write_parquet(seed / "actions.parquet")
    events.write_parquet(seed / "action_events.parquet")
    settings = Settings(
        bundle_dir=tmp_path / "nobundle",
        db_path=tmp_path / "api.duckdb",
        actions_db=tmp_path / "actions.duckdb",
        actions_seed_dir=seed,
        fx_refresh_s=10_000,
        ppi_refresh_s=10_000,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def _schema(name: str) -> dict[str, Any]:
    s = dict(SPEC["components"]["schemas"][name])
    s["$defs"] = SPEC["components"]["schemas"]
    return s


def _validate(payload: Any, schema_name: str) -> None:
    import jsonschema

    jsonschema.validate(payload, _schema(schema_name))


def test_contract_paths_and_queue_shape(client: TestClient) -> None:
    for path in (
        "/actions",
        "/actions/summary",
        "/actions/{action_id}",
        "/actions/{action_id}/submit",
        "/actions/{action_id}/approve",
        "/actions/{action_id}/reject",
        "/actions/{action_id}/rework",
        "/actions/{action_id}/implement",
        "/actions/bulk-approve",
    ):
        assert path in SPEC["paths"], f"{path} missing from openapi.yaml"
    r = client.get("/api/actions")
    assert r.status_code == 200
    _validate(r.json(), "TablePage")
    assert r.json()["total_unfiltered"] == 4
    assert all(row["status"] == "proposed" for row in r.json()["items"])
    s = client.get("/api/actions/summary")
    _validate(s.json(), "ActionsSummary")
    assert s.json()["n_blocked"] == 1
    assert s.json()["n_compliance_flagged"] == 1


def test_lifecycle_self_approval_and_log(client: TestClient) -> None:
    aid = "act_aaa000000001"
    # Viewer may not transition; analyst may not approve.
    assert (
        client.post(f"/api/actions/{aid}/submit", headers={"X-User": "demo-viewer"}).status_code
        == 403
    )
    r = client.post(f"/api/actions/{aid}/submit", headers={"X-User": "demo-lead"})
    assert r.status_code == 200 and r.json()["changed"] is True
    assert (
        client.post(f"/api/actions/{aid}/approve", headers={"X-User": "demo-analyst"}).status_code
        == 403
    )
    # Separation of duties: demo-lead submitted it, so demo-lead cannot approve.
    r = client.post(f"/api/actions/{aid}/approve", headers={"X-User": "demo-lead"})
    assert r.status_code == 403 and r.json()["code"] == "self_approval"
    r = client.post(
        f"/api/actions/{aid}/approve",
        headers={"X-User": "demo-approver"},
        json={"reason": "fits policy"},
    )
    assert r.status_code == 200 and r.json()["changed"] is True
    # Idempotent repeat.
    r = client.post(f"/api/actions/{aid}/approve", headers={"X-User": "demo-approver"})
    assert r.status_code == 200 and r.json()["changed"] is False
    # The event log is complete and valid.
    detail = client.get(f"/api/actions/{aid}").json()
    assert [e["to_status"] for e in detail["events"]] == ["proposed", "under_review", "approved"]
    assert detail["events"][2]["who"] == "demo-approver"
    assert detail["status"] == "approved"


def test_justification_required_for_flagged_approval(client: TestClient) -> None:
    aid = "act_ccc000000003"
    client.post(f"/api/actions/{aid}/submit", headers={"X-User": "demo-analyst"})
    r = client.post(f"/api/actions/{aid}/approve", headers={"X-User": "demo-approver"})
    assert r.status_code == 422 and r.json()["code"] == "justification_required"
    r = client.post(
        f"/api/actions/{aid}/approve",
        headers={"X-User": "demo-approver"},
        json={"reason": "Cost-to-serve difference documented on the compliance detail."},
    )
    assert r.status_code == 200


def test_blocked_action_never_advances(client: TestClient) -> None:
    r = client.post("/api/actions/act_bbb000000002/submit", headers={"X-User": "demo-analyst"})
    assert r.status_code == 409 and r.json()["code"] == "blocked"


def test_bulk_approve_per_item_outcomes(client: TestClient) -> None:
    # Prepare: a1 approved already; a3 flagged and under review; a4 under review.
    client.post("/api/actions/act_aaa000000001/submit", headers={"X-User": "demo-analyst"})
    client.post(
        "/api/actions/act_aaa000000001/approve",
        headers={"X-User": "demo-approver"},
        json={"reason": "ok"},
    )
    client.post("/api/actions/act_ccc000000003/submit", headers={"X-User": "demo-analyst"})
    client.post("/api/actions/act_ddd000000004/submit", headers={"X-User": "demo-analyst"})
    r = client.post(
        "/api/actions/bulk-approve",
        headers={"X-User": "demo-approver"},
        json={
            "action_ids": [
                "act_aaa000000001",  # already approved -> unchanged
                "act_bbb000000002",  # blocked -> refused
                "act_ccc000000003",  # flagged, no note -> refused
                "act_ddd000000004",  # fine -> approved
                "act_zzz000000009",  # unknown -> refused
            ]
        },
    )
    assert r.status_code == 200
    _validate(r.json(), "BulkApproveResult")
    outcomes = {x["action_id"]: x["outcome"] for x in r.json()["results"]}
    assert outcomes == {
        "act_aaa000000001": "unchanged",
        "act_bbb000000002": "refused",
        "act_ccc000000003": "refused",
        "act_ddd000000004": "approved",
        "act_zzz000000009": "refused",
    }
    reasons = {x["action_id"]: x["reason"] for x in r.json()["results"]}
    assert "justification" in reasons["act_ccc000000003"]
    # The refused flagged item's status is untouched (not force-rejected).
    assert client.get("/api/actions/act_ccc000000003").json()["status"] == "under_review"


def test_edits_audited_and_guardrailed(client: TestClient, tmp_path: Path) -> None:
    aid = "act_aaa000000001"
    r = client.patch(
        f"/api/actions/{aid}", headers={"X-User": "demo-analyst"}, json={"owner": "alice"}
    )
    assert r.status_code == 200 and r.json()["changed_fields"] == ["owner"]
    # Price edit below the floor refused.
    r = client.patch(
        f"/api/actions/{aid}",
        headers={"X-User": "demo-analyst"},
        json={"proposed_price_cents": 900},
    )
    assert r.status_code == 422 and r.json()["code"] == "below_floor"
    # Viewer cannot edit.
    assert (
        client.patch(
            f"/api/actions/{aid}", headers={"X-User": "demo-viewer"}, json={"owner": "x"}
        ).status_code
        == 403
    )
    detail = client.get(f"/api/actions/{aid}").json()
    assert detail["action"]["owner"] == "alice"
    assert [e["field"] for e in detail["edits"]] == ["owner"]
    # After approval edits are refused.
    client.post(f"/api/actions/{aid}/submit", headers={"X-User": "demo-analyst"})
    client.post(
        f"/api/actions/{aid}/approve", headers={"X-User": "demo-approver"}, json={"reason": "ok"}
    )
    r = client.patch(
        f"/api/actions/{aid}", headers={"X-User": "demo-analyst"}, json={"owner": "bob"}
    )
    assert r.status_code == 409 and r.json()["code"] == "status_not_editable"


def test_manual_propose_floor_guardrail(client: TestClient) -> None:
    # A price proposal below the pair's floor on record is refused.
    r = client.post(
        "/api/actions",
        headers={"X-User": "demo-analyst"},
        json={
            "action_type": "price_change",
            "title": "Manual raise",
            "rationale": "negotiated",
            "customer_id": "C1",
            "item_id": "I1",
            "proposed_price_cents": 500,
        },
    )
    assert r.status_code == 422 and r.json()["code"] == "below_floor"
    # A pair with no floor on record is refused rather than guessed.
    r = client.post(
        "/api/actions",
        headers={"X-User": "demo-analyst"},
        json={
            "action_type": "price_change",
            "title": "Manual raise",
            "rationale": "negotiated",
            "customer_id": "CX",
            "item_id": "IX",
            "proposed_price_cents": 500,
        },
    )
    assert r.status_code == 422 and r.json()["code"] == "floor_unknown"
    # A valid proposal lands as proposed, with a birth event by the proposer.
    r = client.post(
        "/api/actions",
        headers={"X-User": "demo-analyst"},
        json={
            "action_type": "price_change",
            "title": "Manual raise",
            "rationale": "negotiated",
            "customer_id": "C1",
            "item_id": "I1",
            "proposed_price_cents": 1_500,
        },
    )
    assert r.status_code == 200
    detail = client.get(f"/api/actions/{r.json()['action_id']}").json()
    assert detail["status"] == "proposed"
    assert detail["events"][0]["who"] == "demo-analyst"
    # Viewers cannot propose.
    assert (
        client.post(
            "/api/actions",
            headers={"X-User": "demo-viewer"},
            json={"action_type": "service_policy", "title": "t", "rationale": "r"},
        ).status_code
        == 403
    )


def test_store_log_remains_valid_after_api_drive(client: TestClient, tmp_path: Path) -> None:
    client.post("/api/actions/act_ddd000000004/submit", headers={"X-User": "demo-analyst"})
    client.post(
        "/api/actions/act_ddd000000004/reject",
        headers={"X-User": "demo-approver"},
        json={"reason": "not yet"},
    )
    client.post("/api/actions/act_ddd000000004/rework", headers={"X-User": "demo-analyst"})
    store = ActionStore(tmp_path / "actions.duckdb")
    events = store.events()
    validate_log(events)
    st = replay_status(events)
    assert st.filter(pl.col("action_id") == "act_ddd000000004")["status"][0] == "proposed"


def test_price_export_endpoint(client: TestClient) -> None:
    """Phase 40 over HTTP: dry-run previews, the real call transitions, and a
    repeat is idempotent. Viewer role refused."""
    client.post("/api/actions/act_aaa000000001/submit", headers={"X-User": "demo-analyst"})
    client.post(
        "/api/actions/act_aaa000000001/approve",
        headers={"X-User": "demo-approver"},
        json={"reason": "ok"},
    )
    assert (
        client.post("/api/actions/price-export", headers={"X-User": "demo-viewer"}).status_code
        == 403
    )
    dry = client.post(
        "/api/actions/price-export", headers={"X-User": "demo-analyst"}, json={"dry_run": True}
    ).json()
    assert dry["dry_run"] is True and dry["n_transitioned"] == 0
    assert dry["summary"]["n_changes"] == 1
    assert client.get("/api/actions/act_aaa000000001").json()["status"] == "approved"
    real = client.post(
        "/api/actions/price-export", headers={"X-User": "demo-analyst"}, json={}
    ).json()
    assert real["n_transitioned"] == 1
    assert real["csv"].startswith("ACTION_ID,")
    assert client.get("/api/actions/act_aaa000000001").json()["status"] == "exported"
    again = client.post(
        "/api/actions/price-export", headers={"X-User": "demo-analyst"}, json={}
    ).json()
    assert again["n_transitioned"] == 0 and again["csv"] == real["csv"]
