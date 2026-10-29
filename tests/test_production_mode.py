"""Run 6, Phase 82 (Finding 19): what production mode refuses.

Local mode is a workstation convenience: the client names itself and the
server believes it. That is documented, and it is fine for one person on one
machine. The moment the API is reachable by anyone else it is not identity
at all, so production mode takes every one of those decisions away from the
client.

Each test here is a thing that used to work and must now fail.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api.audit import AuditLog
from api.auth import (
    MODE_PRODUCTION,
    AuthError,
    build_authenticator,
    hash_password,
    parse_accounts_spec,
    set_oidc_verifier,
    verify_password,
)
from api.main import create_app
from api.settings import Settings
from fastapi.testclient import TestClient

PASSWORD = "a long enough passphrase"
OTHER_PASSWORD = "a different passphrase"


@pytest.fixture
def accounts_spec() -> str:
    h1, h2 = (
        hash_password(PASSWORD, iterations=1000),
        hash_password(OTHER_PASSWORD, iterations=1000),
    )
    return (
        f"u-analyst|Ada Analyst|analyst|{h1}\n"
        f"u-approver|Ben Approver|approver|{h2}\n"
        f"u-both|Cass Lead|analyst+approver|{h1}"
    )


def _seed_one_action(seed: Path) -> str:
    """One proposed price action, so the lifecycle can actually be walked."""
    import polars as pl

    from margin_engine.actions.generate import ACTIONS_SCHEMA
    from margin_engine.actions.model import EVENTS_SCHEMA

    action_id = "act_prod00000001"
    row = dict.fromkeys(ACTIONS_SCHEMA)
    row.update(
        {
            "action_id": action_id,
            "action_type": "price_change",
            "title": "Raise C1 / I1 to the segment median",
            "rationale": "below the reference band",
            "evidence_status": "fixed_volume",
            "expected_impact_cents": 10_000,
            "impact_basis": "gross price opportunity",
            "customer_id": "C1",
            "item_id": "I1",
            "owner": "unassigned",
            "priority": "medium",
            "compliance_flags": "",
            "blocked": False,
            "blocked_reason": "",
        }
    )
    seed.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([row]).cast(ACTIONS_SCHEMA).write_parquet(seed / "actions.parquet")
    pl.DataFrame(
        [
            {
                "action_id": action_id,
                "seq": 0,
                "who": "engine",
                "at": datetime(2026, 1, 1, tzinfo=UTC),
                "from_status": None,
                "to_status": "proposed",
                "note": "generated",
            }
        ],
        schema=EVENTS_SCHEMA,
    ).write_parquet(seed / "action_events.parquet")
    return action_id


@pytest.fixture
def prod_app(tmp_path: Path, accounts_spec: str) -> Iterator[TestClient]:
    settings = Settings(
        mode=MODE_PRODUCTION,
        accounts_spec=accounts_spec,
        storage_encrypted=True,
        db_path=tmp_path / "api.duckdb",
        actions_db=tmp_path / "actions.duckdb",
        audit_log=tmp_path / "audit.log",
        uploads_dir=tmp_path / "uploads",
        drift_dir=tmp_path / "drift",
        actions_seed_dir=_seed_dir(tmp_path),
        users_spec="u-analyst:analyst,u-approver:approver,u-both:analyst+approver",
        fx_refresh_s=10_000,
        ppi_refresh_s=10_000,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _seed_dir(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    _seed_one_action(seed)
    return seed


def _login(client: TestClient, user_id: str, password: str = PASSWORD) -> str:
    r = client.post("/api/auth/login", json={"user_id": user_id, "password": password})
    assert r.status_code == 200, r.text
    return str(r.json()["token"])


# ---------------------------------------------------------------------------
# the client does not get to say who it is
# ---------------------------------------------------------------------------


def test_a_spoofed_identity_header_is_rejected_not_ignored(prod_app: TestClient) -> None:
    """Rejected, not ignored: a caller must not be able to believe it worked."""
    r = prod_app.get("/api/auth/me", headers={"X-User": "u-approver"})
    assert r.status_code == 400
    assert r.json()["code"] == "client_identity_rejected"


def test_a_lifecycle_call_cannot_be_made_with_a_header_alone(prod_app: TestClient) -> None:
    r = prod_app.post("/api/actions/anything/approve", headers={"X-User": "u-approver"}, json={})
    assert r.status_code == 400
    assert r.json().get("code") == "client_identity_rejected"


def test_no_token_means_no_identity(prod_app: TestClient) -> None:
    r = prod_app.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


def test_a_made_up_token_is_refused(prod_app: TestClient) -> None:
    r = prod_app.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_token"


def test_a_wrong_scheme_is_refused(prod_app: TestClient) -> None:
    r = prod_app.get("/api/auth/me", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401


def test_logging_in_gives_an_immutable_id_and_a_separate_display_name(
    prod_app: TestClient,
) -> None:
    token = _login(prod_app, "u-analyst")
    me = prod_app.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["user_id"] == "u-analyst"
    assert me["display_name"] == "Ada Analyst"
    assert me["auth_method"] == "password"


def test_logging_out_ends_the_session(prod_app: TestClient) -> None:
    token = _login(prod_app, "u-analyst")
    auth = {"Authorization": f"Bearer {token}"}
    assert prod_app.get("/api/auth/me", headers=auth).json()["authenticated"] is True
    prod_app.post("/api/auth/logout", headers=auth)
    assert prod_app.get("/api/auth/me", headers=auth).status_code == 401


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------


def test_an_unknown_user_and_a_wrong_password_look_the_same(prod_app: TestClient) -> None:
    a = prod_app.post("/api/auth/login", json={"user_id": "nobody", "password": PASSWORD})
    b = prod_app.post("/api/auth/login", json={"user_id": "u-analyst", "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


def test_an_account_with_no_password_cannot_be_logged_into() -> None:
    assert verify_password(None, "anything") is False
    assert verify_password("", "anything") is False
    accounts = parse_accounts_spec("u1|No Password|viewer")
    assert accounts["u1"].password_hash is None


def test_a_hash_does_not_contain_the_password() -> None:
    h = hash_password(PASSWORD, iterations=1000)
    assert PASSWORD not in h
    assert h.startswith("pbkdf2_sha256$1000$")
    assert verify_password(h, PASSWORD) and not verify_password(h, PASSWORD + "!")


def test_two_hashes_of_one_password_differ() -> None:
    assert hash_password(PASSWORD, iterations=1000) != hash_password(PASSWORD, iterations=1000)


def test_a_malformed_stored_hash_fails_rather_than_passing() -> None:
    for bad in ("", "nonsense", "pbkdf2_sha256$notanint$aa$bb", "md5$1$aa$bb"):
        assert verify_password(bad, PASSWORD) is False


# ---------------------------------------------------------------------------
# separation of duties, on ids
# ---------------------------------------------------------------------------


def test_separation_of_duties_is_decided_on_the_immutable_id() -> None:
    """A display name can be changed by the person it would constrain; the id
    is what the rule has to be written against."""
    from margin_engine.actions.review import ReviewPolicy

    policy = ReviewPolicy.from_spec("u-both:analyst+approver")
    assert policy.roles("u-both") == {"analyst", "approver"}
    # The id, not "Cass Lead", is what reaches the policy.
    h = hash_password(PASSWORD, iterations=1000)
    auth = build_authenticator(
        mode=MODE_PRODUCTION,
        users_spec="",
        accounts_spec=f"u-both|Cass Lead|analyst+approver|{h}",
        session_ttl_s=60,
    )
    token, _, principal = auth.login("u-both", PASSWORD)
    assert principal.user_id == "u-both"
    assert auth.resolve(header_user=None, authorization=f"Bearer {token}").user_id == "u-both"


# ---------------------------------------------------------------------------
# uploads belong to whoever sent them
# ---------------------------------------------------------------------------


def test_another_users_upload_cannot_be_read(prod_app: TestClient) -> None:
    mine = _login(prod_app, "u-analyst")
    theirs = _login(prod_app, "u-approver", OTHER_PASSWORD)
    body = b"INV,CUST,ITEM\n1,C1,I1\n"
    up = prod_app.post(
        "/api/load/upload?filename=export.csv",
        content=body,
        headers={"Authorization": f"Bearer {mine}"},
    )
    assert up.status_code == 200, up.text
    upload_id = up.json()["upload_id"]
    # The id is a content hash, not a secret: knowing it is not permission.
    r = prod_app.post(
        f"/api/load/profile/{upload_id}", headers={"Authorization": f"Bearer {theirs}"}
    )
    assert r.status_code == 404
    ok = prod_app.post(
        f"/api/load/profile/{upload_id}", headers={"Authorization": f"Bearer {mine}"}
    )
    assert ok.status_code == 200, ok.text


def test_another_users_upload_cannot_be_deleted(prod_app: TestClient) -> None:
    mine = _login(prod_app, "u-analyst")
    theirs = _login(prod_app, "u-approver", OTHER_PASSWORD)
    up = prod_app.post(
        "/api/load/upload?filename=export.csv",
        content=b"INV,CUST\n1,C1\n",
        headers={"Authorization": f"Bearer {mine}"},
    )
    upload_id = up.json()["upload_id"]
    assert (
        prod_app.delete(
            f"/api/load/upload/{upload_id}", headers={"Authorization": f"Bearer {theirs}"}
        ).status_code
        == 404
    )
    assert (
        prod_app.delete(
            f"/api/load/upload/{upload_id}", headers={"Authorization": f"Bearer {mine}"}
        ).status_code
        == 200
    )


def test_retention_deletes_what_nobody_came_back_for(tmp_path: Path) -> None:
    import os
    import time

    from api.load_routes import sweep_expired_uploads

    root = tmp_path / "uploads"
    (root / "old").mkdir(parents=True)
    (root / "new").mkdir(parents=True)
    old_file = root / "old" / "export.csv"
    old_file.write_text("a,b\n", encoding="utf-8")
    (root / "new" / "export.csv").write_text("a,b\n", encoding="utf-8")
    ancient = time.time() - 30 * 86_400
    os.utime(old_file, (ancient, ancient))

    assert sweep_expired_uploads(root, retention_days=7) == 1
    assert not (root / "old").exists()
    assert (root / "new").exists()
    # Zero means keep, and says so by doing nothing.
    assert sweep_expired_uploads(root, retention_days=0) == 0


# ---------------------------------------------------------------------------
# the audit log keeps the record and not the secrets
# ---------------------------------------------------------------------------


def test_the_audit_log_records_who_and_what(prod_app: TestClient, tmp_path: Path) -> None:
    token = _login(prod_app, "u-analyst")
    prod_app.post(
        "/api/load/upload?filename=export.csv",
        content=b"INV,CUST\n1,C1\n",
        headers={"Authorization": f"Bearer {token}"},
    )
    events = AuditLog(tmp_path / "audit.log").read()
    actions = {e["action"] for e in events}
    assert "auth.login" in actions
    assert "load.upload" in actions
    assert any(e["user_id"] == "u-analyst" for e in events)


def test_a_failed_login_is_recorded_without_what_was_tried(
    prod_app: TestClient, tmp_path: Path
) -> None:
    prod_app.post("/api/auth/login", json={"user_id": "u-analyst", "password": "hunter2"})
    text = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "invalid_credentials" in text
    assert "hunter2" not in text


def test_the_audit_log_holds_no_secrets(prod_app: TestClient, tmp_path: Path) -> None:
    token = _login(prod_app, "u-analyst")
    prod_app.post(
        "/api/load/upload?filename=export.csv",
        content=b"SECRET-CUSTOMER-NAME,99.99\n",
        headers={"Authorization": f"Bearer {token}"},
    )
    text = (tmp_path / "audit.log").read_text(encoding="utf-8")
    for secret in (PASSWORD, token, "SECRET-CUSTOMER-NAME", "pbkdf2_sha256"):
        assert secret not in text, f"the audit log leaked {secret[:12]!r}"


def test_the_log_refuses_a_field_that_should_never_be_written(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.log")
    with pytest.raises(ValueError, match="refusing to write"):
        log.record(action="x", user_id="u", status=200, password="hunter2")
    with pytest.raises(ValueError, match="refusing to write"):
        log.record(action="x", user_id="u", status=200, token="abc")


def test_log_lines_are_json_and_append_only(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.log")
    log.record(action="one", user_id="u", status=200)
    log.record(action="two", user_id="u", status=403, detail="nope")
    lines = (tmp_path / "a.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(ln)["action"] for ln in lines] == ["one", "two"]


# ---------------------------------------------------------------------------
# a production deployment that cannot be used is refused at startup
# ---------------------------------------------------------------------------


def test_production_without_accounts_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="MARGIN_API_ACCOUNTS"):
        build_authenticator(
            mode=MODE_PRODUCTION, users_spec="a:analyst", accounts_spec="", session_ttl_s=60
        )


def test_production_without_any_way_to_authenticate_refuses_to_start() -> None:
    set_oidc_verifier(None)
    with pytest.raises(ValueError, match="no account with a password hash"):
        build_authenticator(
            mode=MODE_PRODUCTION,
            users_spec="",
            accounts_spec="u1|No Password|analyst",
            session_ttl_s=60,
        )


def test_production_without_declared_encrypted_storage_refuses_to_start(
    tmp_path: Path, accounts_spec: str
) -> None:
    """The engine cannot verify the volume from inside the process, so the
    operator declares it. A silent assumption would be worse."""
    settings = Settings(
        mode=MODE_PRODUCTION,
        accounts_spec=accounts_spec,
        storage_encrypted=False,
        db_path=tmp_path / "a.duckdb",
        actions_db=tmp_path / "b.duckdb",
        audit_log=tmp_path / "audit.log",
        uploads_dir=tmp_path / "uploads",
    )
    with pytest.raises(RuntimeError, match="MARGIN_API_STORAGE_ENCRYPTED"):
        create_app(settings)


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="MARGIN_API_MODE"):
        build_authenticator(
            mode="staging", users_spec="a:analyst", accounts_spec="", session_ttl_s=60
        )


def test_an_account_with_an_unknown_role_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        parse_accounts_spec("u1|Name|wizard|pbkdf2_sha256$1$aa$bb")


# ---------------------------------------------------------------------------
# the OIDC seam
# ---------------------------------------------------------------------------


def test_an_oidc_verifier_takes_over_token_resolution() -> None:
    h = hash_password(PASSWORD, iterations=1000)
    auth = build_authenticator(
        mode=MODE_PRODUCTION,
        users_spec="",
        accounts_spec=f"u1|One|analyst|{h}",
        session_ttl_s=60,
    )
    set_oidc_verifier(lambda token: ("oidc-user", "From The Provider", frozenset({"approver"})))
    try:
        principal = auth.resolve(header_user=None, authorization="Bearer provider-token")
        assert principal is not None
        assert principal.user_id == "oidc-user"
        assert principal.auth_method == "oidc"
    finally:
        set_oidc_verifier(None)


def test_a_provider_failure_is_a_401_not_a_500() -> None:
    h = hash_password(PASSWORD, iterations=1000)
    auth = build_authenticator(
        mode=MODE_PRODUCTION, users_spec="", accounts_spec=f"u1|One|analyst|{h}", session_ttl_s=60
    )

    def boom(_token: str) -> tuple[str, str, frozenset[str]]:
        raise RuntimeError("provider unreachable")

    set_oidc_verifier(boom)
    try:
        with pytest.raises(AuthError) as caught:
            auth.resolve(header_user=None, authorization="Bearer x")
        assert caught.value.status == 401
        assert "provider unreachable" not in caught.value.message
    finally:
        set_oidc_verifier(None)


# ---------------------------------------------------------------------------
# local mode is unchanged
# ---------------------------------------------------------------------------


def test_local_mode_still_takes_the_header_and_says_what_it_is(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "a.duckdb",
        actions_db=tmp_path / "b.duckdb",
        audit_log=tmp_path / "audit.log",
        uploads_dir=tmp_path / "uploads",
    )
    with TestClient(create_app(settings)) as client:
        me = client.get("/api/auth/me", headers={"X-User": "demo-analyst"}).json()
        assert me["mode"] == "local"
        assert me["authenticated"] is True
        assert me["auth_method"] == "local-identity-header"
        anon = client.get("/api/auth/me").json()
        assert anon["authenticated"] is False
        assert "not authentication" in anon["note"]
        # Logging in is not a thing here, and the answer explains why.
        r = client.post("/api/auth/login", json={"user_id": "x", "password": "y"})
        assert r.status_code == 409
        assert r.json()["code"] == "not_production_mode"


# ---------------------------------------------------------------------------
# the lifecycle, walked with tokens
# ---------------------------------------------------------------------------

ACTION_ID = "act_prod00000001"


def test_self_approval_is_refused_on_the_server_not_in_the_browser(
    prod_app: TestClient,
) -> None:
    """The analyst who submitted it holds the approver role too, and still
    cannot approve it. The check runs server-side against the id the server
    issued, so no client can route around it."""
    token = _login(prod_app, "u-both")
    auth = {"Authorization": f"Bearer {token}"}
    submitted = prod_app.post(f"/api/actions/{ACTION_ID}/submit", headers=auth, json={})
    assert submitted.status_code == 200, submitted.text
    denied = prod_app.post(
        f"/api/actions/{ACTION_ID}/approve", headers=auth, json={"reason": "looks fine to me"}
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "self_approval"


def test_a_different_approver_can_approve_what_another_submitted(prod_app: TestClient) -> None:
    analyst = {"Authorization": f"Bearer {_login(prod_app, 'u-analyst')}"}
    approver = {"Authorization": f"Bearer {_login(prod_app, 'u-approver', OTHER_PASSWORD)}"}
    assert (
        prod_app.post(f"/api/actions/{ACTION_ID}/submit", headers=analyst, json={}).status_code
        == 200
    )
    ok = prod_app.post(
        f"/api/actions/{ACTION_ID}/approve", headers=approver, json={"reason": "checked the band"}
    )
    assert ok.status_code == 200, ok.text


def test_a_viewer_token_cannot_move_an_action(prod_app: TestClient) -> None:
    approver = {"Authorization": f"Bearer {_login(prod_app, 'u-approver', OTHER_PASSWORD)}"}
    # The approver has no analyst role, so submitting is not theirs to do.
    r = prod_app.post(f"/api/actions/{ACTION_ID}/submit", headers=approver, json={})
    assert r.status_code == 403
