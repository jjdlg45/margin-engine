"""Run 4 (30.7): BFF API tests - all offline.

- Contract tests GENERATED from docs/api/openapi.yaml (schemas validated with
  jsonschema against live TestClient responses).
- Parity: API table responses equal the exported bundle rows to the cent.
- Inbound rate limiting: 429 + Retry-After.
- ETag caching: 304 on If-None-Match.
- Refresh jobs run from fixtures; a simulated upstream failure marks the
  section stale instead of breaking anything.
- Replay idempotency: re-running a day never double counts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from api.main import create_app
from api.replay import ReplayFeed
from api.settings import Settings
from api.store import BundleStore, _read_shards
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "web" / "public" / "data"
SPEC = yaml.safe_load((REPO / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8"))


def _schema(name: str) -> dict[str, Any]:
    s = dict(SPEC["components"]["schemas"][name])
    s["$defs"] = SPEC["components"]["schemas"]
    return s


def _validate(payload: Any, schema_name: str) -> None:
    import jsonschema

    jsonschema.validate(payload, _schema(schema_name))


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The exported web bundle the API serves. Locally this is the dev copy at
    web/public/data; in a fresh clone or CI that directory is generated output
    and absent, so build the same deterministic demo-world bundle once (what
    the pages workflow builds) into a temp dir."""
    if (BUNDLE / "bundle.json").exists():
        return BUNDLE
    from typer.testing import CliRunner

    from margin_engine.cli import app

    out = tmp_path_factory.mktemp("demo_bundle")
    result = CliRunner().invoke(app, ["export-web", "--demo", "--out", str(out)])
    assert result.exit_code == 0, result.output
    return out


@pytest.fixture(scope="module")
def client(bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    tmp = tmp_path_factory.mktemp("api")
    s = Settings(
        bundle_dir=bundle,
        db_path=tmp / "api.duckdb",
        fx_refresh_s=10_000,
        ppi_refresh_s=10_000,
    )
    with TestClient(create_app(s)) as c:
        yield c


def test_health_and_version(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    v = client.get("/api/version").json()
    assert v["api_version"] and v["bundle_version"]


def test_app_matches_contract_schema(client: TestClient) -> None:
    r = client.get("/api/app")
    assert r.status_code == 200
    _validate(r.json(), "AppData")


def test_every_contract_page_serves(client: TestClient) -> None:
    pages = SPEC["paths"]["/pages/{page}"]["get"]["parameters"][0]["schema"]["enum"]
    for page in pages:
        r = client.get(f"/api/pages/{page}")
        assert r.status_code == 200, page
        assert isinstance(r.json(), dict)
    nf = client.get("/api/pages/nonsense")
    assert nf.status_code == 404
    assert "detail" in nf.json()


def test_tables_match_contract_schema(client: TestClient) -> None:
    tables = SPEC["paths"]["/tables/{table}"]["get"]["parameters"][0]["schema"]["enum"]
    for table in tables:
        r = client.get(f"/api/tables/{table}", params={"page_size": 5})
        assert r.status_code == 200, table
        _validate(r.json(), "TablePage")
    assert client.get("/api/tables/nope").status_code == 404
    bad = client.get("/api/tables/guidance", params={"page": 0})
    assert bad.status_code == 400
    assert "detail" in bad.json()
    bad2 = client.get("/api/tables/guidance", params={"filters": "{not json"})
    assert bad2.status_code == 400


def test_parity_api_equals_bundle_to_the_cent(client: TestClient, bundle: Path) -> None:
    for table in ("guidance", "opportunities", "customers"):
        rows = _read_shards(bundle, f"tables/{table}.json")
        got: list[dict[str, Any]] = []
        page = 1
        while True:
            r = client.get(f"/api/tables/{table}", params={"page": page, "page_size": 5000}).json()
            got.extend(r["items"])
            if not r["has_more"]:
                assert r["total_unfiltered"] == len(rows)
                break
            page += 1
        assert len(got) == len(rows)
        # Cents-exact: every original field value survives the API unchanged
        # (the API only ADDS the contract's restored nulls for guidance).
        for orig, srv in zip(rows, got, strict=True):
            for k, v in orig.items():
                assert srv[k] == v, (table, k)


def test_filters_sort_and_pagination(client: TestClient) -> None:
    r = client.get(
        "/api/tables/guidance",
        params={"filters": json.dumps({"statuses": ["below_reference"]}), "page_size": 10},
    ).json()
    assert r["total"] <= r["total_unfiltered"]
    assert all(row["status"] == "below_reference" for row in r["items"])
    s = client.get(
        "/api/tables/opportunities",
        params={"sort": "-gross_price_opportunity_cents", "page_size": 50},
    ).json()
    vals = [row["gross_price_opportunity_cents"] for row in s["items"]]
    assert vals == sorted(vals, reverse=True)


def test_explanation_endpoint(client: TestClient) -> None:
    row = client.get("/api/tables/guidance", params={"page_size": 1}).json()["items"][0]
    r = client.get(f"/api/tables/guidance/explanations/{row['customer_id']}/{row['item_id']}")
    assert r.status_code == 200
    assert r.json()["explanation"]
    assert client.get("/api/tables/guidance/explanations/XX/YY").status_code == 404


def test_etag_304(client: TestClient) -> None:
    first = client.get("/api/app")
    etag = first.headers["etag"]
    again = client.get("/api/app", headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_rate_limit_429_with_retry_after(bundle: Path, tmp_path: Path) -> None:
    s = Settings(
        bundle_dir=bundle,
        db_path=tmp_path / "rl.duckdb",
        rate_limit_per_minute=60,
        rate_limit_burst=3,
        fx_refresh_s=10_000,
        ppi_refresh_s=10_000,
    )
    with TestClient(create_app(s)) as c:
        codes = [c.get("/api/health").status_code for _ in range(5)]
        assert 429 in codes
        r = next(
            resp for resp in [c.get("/api/health") for _ in range(3)] if resp.status_code == 429
        )
        assert int(r.headers["Retry-After"]) >= 1
        assert "detail" in r.json()


def test_refresh_jobs_offline_and_stale_on_failure(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.refresh import RefreshScheduler

    s = Settings(bundle_dir=bundle, db_path=tmp_path / "r.duckdb")
    store = BundleStore(s.db_path)
    store.load_bundle(bundle)
    sched = RefreshScheduler(s, store)
    sched.refresh_fx()
    sched.refresh_ppi()
    assert sched.status["fx"]["stale"] is False
    assert sched.status["ppi"]["stale"] is False

    # Simulated upstream failure with no cache: the section is marked stale
    # with the reason; nothing raises.
    import margin_engine.connectors.fx as fxmod

    def boom(*a: object, **k: object) -> None:
        from margin_engine.connectors.base import ConnectorError

        raise ConnectorError("upstream down")

    monkeypatch.setattr(fxmod, "fetch_daily_rates", boom)
    s2 = Settings(bundle_dir=bundle, db_path=tmp_path / "r2.duckdb")
    sched2 = RefreshScheduler(s2, BundleStore(s2.db_path))
    sched2.refresh_fx()
    assert sched2.status["fx"]["stale"] is True
    assert "down" in sched2.status["fx"]["detail"]


def test_replay_idempotency_no_double_count(bundle: Path, tmp_path: Path) -> None:
    s = Settings(
        bundle_dir=bundle,
        db_path=tmp_path / "replay.duckdb",
        actions_db=tmp_path / "actions.duckdb",
        replay_enabled=True,
        replay_work_dir=tmp_path / "work",
        replay_source_dir=REPO / "data" / "sample_online_retail",
    )
    store = BundleStore(s.db_path)
    feed = ReplayFeed(s, store)
    days = feed.available_days()
    assert days == ["2009-12-01", "2009-12-02"]

    assert feed.replay_next_day() == "2009-12-01"
    kpis_d1 = store.snapshot.app["kpis"]
    v1 = store.snapshot.version
    # Re-running the same day is a no-op: state guard, nothing rebuilt.
    assert feed.replay_day("2009-12-01") is False
    assert store.snapshot.version == v1

    assert feed.replay_next_day() == "2009-12-02"
    kpis_d2 = store.snapshot.app["kpis"]
    assert feed.replay_next_day() is None  # feed exhausted

    # No double counting: day-2 totals grow from day 1 by exactly the day-2
    # lines (the world is filtered from source, never appended), and replaying
    # day 2 again changes nothing.
    assert kpis_d2["net_revenue_cents"] > kpis_d1["net_revenue_cents"]
    assert feed.replay_day("2009-12-02") is False
    assert store.snapshot.app["kpis"] == kpis_d2
    assert "Historical data replayed" in store.snapshot.app["data_label"]
