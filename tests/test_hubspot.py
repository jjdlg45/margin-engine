"""Run 7, Phase 93: the HubSpot CRM connector.

Offline tests run against fixtures BUILT FROM THE DOCUMENTED CRM v3 API
SCHEMA and are labeled as such - no developer-account credentials existed
when they were written. The opt-in `live` test runs a real sync against the
owner's test account and skips cleanly when the token is absent.
"""

from __future__ import annotations

import json
import urllib.parse
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from margin_engine.connectors.credentials import CredentialStore
from margin_engine.connectors.framework import SyncStore, run_sync
from margin_engine.connectors.hubspot import HubSpotConnector

# ---- documented-schema fixtures (labeled: schema-derived, not recorded) -------

PIPELINES = {
    "results": [
        {
            "id": "default",
            "stages": [
                {"id": "appointmentscheduled", "metadata": {"isClosed": "false"}},
                {
                    "id": "closedwon",
                    "metadata": {"isClosed": "true", "probability": "1.0"},
                },
                {
                    "id": "closedlost",
                    "metadata": {"isClosed": "true", "probability": "0.0"},
                },
            ],
        }
    ]
}
COMPANIES = [
    {"id": "501", "properties": {"name": "Riverside HVAC"}, "archived": False},
    {"id": "502", "properties": {"name": "Delta Plumbing"}, "archived": False},
]


def _deal(
    deal_id: str,
    stage: str,
    company: str | None,
    line_items: list[str],
    created: str = "2026-08-01T10:00:00Z",
) -> dict[str, Any]:
    assoc: dict[str, Any] = {}
    if company:
        assoc["companies"] = {"results": [{"id": company, "type": "deal_to_company"}]}
    if line_items:
        # The v3 list response keys this block "line items" (object type name).
        assoc["line items"] = {
            "results": [{"id": i, "type": "deal_to_line_item"} for i in line_items]
        }
    return {
        "id": deal_id,
        "properties": {
            "dealname": f"Deal {deal_id}",
            "dealstage": stage,
            "pipeline": "default",
            "createdate": created,
            "closedate": "2026-09-01T12:00:00Z",
            "hs_lastmodifieddate": "2026-09-01T12:00:00Z",
        },
        "associations": assoc,
        "archived": False,
    }


DEALS = [
    _deal("9001", "closedwon", "501", ["71", "72"]),
    _deal("9002", "closedlost", "502", ["73"]),
    _deal("9003", "appointmentscheduled", "501", ["74"]),  # open -> excluded
    _deal("9004", "closedwon", None, ["75"]),  # no company -> excluded
    _deal("9005", "closedwon", "501", []),  # no line items -> excluded
    _deal("9006", "mystery_stage", "501", ["76"]),  # unknown stage -> excluded
]
LINE_ITEMS = {
    "71": {"quantity": 10, "price": Decimal("12.50"), "hs_product_id": "P-1", "name": "Pump"},
    "72": {"quantity": 2, "price": Decimal("99.00"), "hs_product_id": "P-2", "name": "Valve"},
    "73": {"quantity": 5, "price": Decimal("40.00"), "hs_product_id": "P-1", "name": "Pump"},
}


class FakeHubSpot:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.deals = [json.loads(json.dumps(d)) for d in DEALS]
        self.companies = [json.loads(json.dumps(c)) for c in COMPANIES]
        self.archived_deals: list[dict[str, Any]] = []
        self.archived_companies: list[dict[str, Any]] = []
        self.changed_deal_ids: list[str] = []
        self.fail_429_once = False

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, Any]:
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        payload = json.loads(body) if body else None
        self.requests.append((method, url, payload))
        assert headers["Authorization"] == "Bearer fake-hubspot-token"
        if self.fail_429_once:
            self.fail_429_once = False
            return 429, {"message": "rate limited", "_retry_after": "0"}
        path = parsed.path
        if path == "/crm/v3/pipelines/deals":
            return 200, PIPELINES
        if path == "/crm/v3/objects/companies":
            rows = self.archived_companies if q.get("archived") == ["true"] else self.companies
            return 200, {"results": rows}
        if path == "/crm/v3/objects/deals":
            rows = self.archived_deals if q.get("archived") == ["true"] else self.deals
            return 200, {"results": rows}
        if path.startswith("/crm/v3/objects/deals/") and method == "GET":
            deal_id = path.rsplit("/", 1)[1]
            match = next((d for d in self.deals if d["id"] == deal_id), None)
            return (200, match) if match else (404, {"message": "not found"})
        if path == "/crm/v3/objects/deals/search":
            return 200, {"results": [{"id": i} for i in self.changed_deal_ids]}
        if path == "/crm/v3/objects/line_items/batch/read":
            assert payload is not None
            results = [
                {"id": i["id"], "properties": LINE_ITEMS[i["id"]]}
                for i in payload["inputs"]
                if i["id"] in LINE_ITEMS
            ]
            return 200, {"results": results}
        return 404, {"message": f"no fixture for {path}"}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HUBSPOT_TOKEN", "fake-hubspot-token")
    return tmp_path


def _connector(fake: FakeHubSpot) -> HubSpotConnector:
    return HubSpotConnector(CredentialStore(), transport=fake, retry_wait_s=0.0)


def test_full_sync_decided_deals_become_quotes(env: Path) -> None:
    fake = FakeHubSpot()
    manifest = run_sync(_connector(fake), env / "state")
    assert manifest["records"]["customers"]["rows"] == 2
    assert manifest["records"]["quotes"]["rows"] == 3
    assert manifest["records"]["quote_exclusions"]["rows"] == 4

    sync_dir = SyncStore(env / "state", "hubspot").latest_dir()
    assert sync_dir is not None
    quotes = pl.read_csv(sync_dir / "quotes.csv", infer_schema=False)
    by_line = {r["LINE_ID"]: r for r in quotes.to_dicts()}
    assert by_line["71"]["OUTCOME"] == "won"
    assert by_line["71"]["CUSTOMER_ID"] == "501"
    assert by_line["71"]["QUOTE_PRC"] == "12.50"
    assert by_line["71"]["QUOTE_DT"] == "2026-08-01"
    assert by_line["73"]["OUTCOME"] == "lost"

    exclusions = pl.read_csv(sync_dir / "quote_exclusions.csv", infer_schema=False)
    reason_of = {r["QUOTE_ID"]: r["REASON"] for r in exclusions.to_dicts()}
    assert reason_of == {
        "9003": "open",
        "9004": "no_company",
        "9005": "no_line_items",
        "9006": "stage_not_recognized",
    }
    assert any("closed-won and closed-lost" in w for w in manifest["warnings"])

    names = pl.read_csv(sync_dir / "customers.csv", infer_schema=False)
    assert set(names["COMPANY_NAME"].to_list()) == {"Riverside HVAC", "Delta Plumbing"}


def test_incremental_search_and_archival_tombstones(env: Path) -> None:
    fake = FakeHubSpot()
    run_sync(_connector(fake), env / "state")

    # Deal 9003 closes as won; deal 9002 is archived (deleted) in HubSpot.
    for d in fake.deals:
        if d["id"] == "9003":
            d["properties"]["dealstage"] = "closedwon"
            d["associations"]["line items"] = {"results": [{"id": "73", "type": "x"}]}
    fake.changed_deal_ids = ["9003"]
    fake.archived_deals = [{"id": "9002", "properties": {}, "archived": True}]
    manifest = run_sync(_connector(fake), env / "state")

    search_calls = [r for r in fake.requests if r[1].endswith("/deals/search")]
    assert search_calls, "incremental sync uses the search endpoint"
    filters = search_calls[0][2]["filterGroups"][0]["filters"][0]
    assert filters["propertyName"] == "hs_lastmodifieddate"
    assert filters["operator"] == "GTE"

    sync_dir = SyncStore(env / "state", "hubspot").latest_dir()
    assert sync_dir is not None
    quotes = pl.read_csv(sync_dir / "quotes.csv", infer_schema=False)
    by_line = {r["LINE_ID"]: r for r in quotes.to_dicts()}
    assert by_line["73"]["QUOTE_ID"] == "9003", "newly decided deal arrived"
    assert by_line["73"]["OUTCOME"] == "won"
    assert all(r["QUOTE_ID"] != "9002" for r in quotes.to_dicts()), "archived deal removed"

    exclusions = pl.read_csv(sync_dir / "quote_exclusions.csv", infer_schema=False)
    assert "9003" not in exclusions["QUOTE_ID"].to_list(), "no longer an exclusion"
    assert manifest["records"]["quotes"]["deleted"] >= 1


def test_429_honors_retry_after(env: Path) -> None:
    fake = FakeHubSpot()
    fake.fail_429_once = True
    result = _connector(fake).check()
    assert result.ok, result.message


def test_check_messages(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHubSpot()
    message = _connector(fake).check().message
    assert "deal stage" in message
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    result = _connector(FakeHubSpot()).check()
    assert not result.ok
    assert "HUBSPOT_TOKEN" in result.message


def test_quotes_merge_into_engine_quotes_table(env: Path, tmp_path: Path) -> None:
    """The materialized quotes.csv loads through a mapping's quotes
    reference-table declaration (the merge pattern docs/connectors.md
    describes for CRM data next to an ERP export)."""
    from margin_engine.ingest.loader import load_reference_table
    from margin_engine.ingest.mapping import load_mapping
    from tests.fixtures.builder import FixtureWorld

    fake = FakeHubSpot()
    run_sync(_connector(fake), env / "state")
    sync_dir = SyncStore(env / "state", "hubspot").latest_dir()
    assert sync_dir is not None

    world_dir = tmp_path / "world"
    world = FixtureWorld(tmp=world_dir)
    world.add_item("P-1")
    world.add_line(INV_NO="I1", CUST_NO="501", ITEM_NO="P-1", QTY_SHP="1", EXT_NET="10.00")
    world.write()
    for f in ("quotes.csv", "customers.csv"):
        (world_dir / f).write_bytes((sync_dir / f).read_bytes())

    mapping_text = """
source_system: erp_plus_hubspot
lines_file: invoice_lines.csv
decimal_separator: "."
lines:
  source_line_key: {column: LINE_KEY}
  invoice_id: {column: INV_NO}
  invoice_date: {column: INV_DT, format: "%m/%d/%Y"}
  customer_id: {column: CUST_NO}
  item_id: {column: ITEM_NO}
  quantity: {column: QTY_SHP}
  net_amount:
    column: EXT_NET
    currency: USD
    sign_convention: natural
    discounts_embedded: true
reference_tables:
  customers:
    file: customers.csv
    columns: {customer_id: COMPANY_ID, customer_name: COMPANY_NAME}
  quotes:
    file: quotes.csv
    date_format: "%Y-%m-%d"
    columns:
      quote_id: QUOTE_ID
      quote_date: QUOTE_DT
      customer_id: CUSTOMER_ID
      item_id: ITEM_ID
      quantity: QTY
      quoted_price: QUOTE_PRC
      outcome: OUTCOME
"""
    mapping_path = tmp_path / "merged.yaml"
    mapping_path.write_text(mapping_text, encoding="utf-8")
    mapping = load_mapping(mapping_path)
    quotes = load_reference_table(world_dir, mapping, "quotes")
    assert quotes is not None
    assert quotes.height == 3
    assert set(quotes["outcome"].to_list()) == {"won", "lost"}
    customers = load_reference_table(world_dir, mapping, "customers")
    assert customers is not None
    assert customers.height == 2


@pytest.mark.live
def test_live_hubspot_sync(tmp_path: Path) -> None:
    """Opt-in: a full sync against the owner's HubSpot developer test
    account. Skips cleanly when the token is absent."""
    creds = CredentialStore()
    if not creds.get("HUBSPOT_TOKEN"):
        pytest.skip("HubSpot private app token not configured")
    connector = HubSpotConnector(creds)
    check = connector.check()
    assert check.ok, check.message
    manifest = run_sync(connector, tmp_path / "state")
    assert manifest["records"]["customers"]["rows"] >= 0
