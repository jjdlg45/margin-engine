"""Run 7, Phase 92: the QuickBooks Online connector.

Offline tests run against fixtures BUILT FROM THE DOCUMENTED API SCHEMA
(Intuit Accounting API, minorversion 75) and are labeled as such - no
sandbox credentials existed when they were written, so they encode the
documented shapes, not recorded responses. The opt-in `live` test runs a
real sync against the owner's sandbox and then an engine run on the result;
it skips cleanly when credentials are absent.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

import margin_engine.connectors.quickbooks as qbo
from margin_engine.connectors.base import ConnectorError
from margin_engine.connectors.credentials import CredentialStore
from margin_engine.connectors.framework import SyncStore, run_sync
from margin_engine.connectors.quickbooks import (
    TOKEN_URL,
    QuickBooksConnector,
    parse_profit_and_loss,
)
from margin_engine.ingest.loader import load_lines, load_reference_table
from margin_engine.ingest.mapping import load_mapping

MAPPING = "config/mappings/quickbooks_online.yaml"

# ---- documented-schema fixtures (labeled: schema-derived, not recorded) -------

CUSTOMERS = [
    {"Id": "1", "DisplayName": "Amy's Bird Sanctuary"},
    {"Id": "2", "DisplayName": "Cool Cars"},
]
ITEMS = [
    {"Id": "11", "Name": "Pump", "Type": "Inventory", "PurchaseCost": 10.50},
    {"Id": "12", "Name": "Consulting", "Type": "Service"},  # no purchase cost
]
INVOICES = [
    {
        "Id": "130",
        "DocNumber": "1037",
        "TxnDate": "2026-09-05",
        "CustomerRef": {"value": "1"},
        "CurrencyRef": {"value": "USD"},
        "Line": [
            {
                "Id": "1",
                "DetailType": "SalesItemLineDetail",
                "Amount": 105.00,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "11"},
                    "Qty": 10,
                    "UnitPrice": 10.50,
                },
            },
            {
                "Id": "2",
                "DetailType": "SalesItemLineDetail",
                "Amount": 75.00,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "12"},
                    "Qty": 1,
                    "UnitPrice": 75.00,
                },
            },
            {"DetailType": "SubTotalLineDetail", "Amount": 180.00},
        ],
    }
]
CREDIT_MEMOS = [
    {
        "Id": "45",
        "DocNumber": "CM-9",
        "TxnDate": "2026-09-20",
        "CustomerRef": {"value": "1"},
        "Line": [
            {
                "Id": "1",
                "DetailType": "SalesItemLineDetail",
                "Amount": 21.00,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "11"},
                    "Qty": 2,
                    "UnitPrice": 10.50,
                },
            }
        ],
    }
]
PNL_REPORT = {
    "Columns": {
        "Column": [
            {"ColTitle": "", "ColType": "Account"},
            {
                "ColTitle": "Sep 2026",
                "ColType": "Money",
                "MetaData": [
                    {"Name": "StartDate", "Value": "2026-09-01"},
                    {"Name": "EndDate", "Value": "2026-09-30"},
                ],
            },
            {
                "ColTitle": "Oct 2026",
                "ColType": "Money",
                "MetaData": [
                    {"Name": "StartDate", "Value": "2026-10-01"},
                    {"Name": "EndDate", "Value": "2026-10-31"},
                ],
            },
        ]
    },
    "Rows": {
        "Row": [
            {
                "group": "Income",
                "Header": {"ColData": [{"value": "Income"}]},
                "Summary": {
                    "ColData": [
                        {"value": "Total Income"},
                        {"value": "159.00"},
                        {"value": "0.00"},
                    ]
                },
                "type": "Section",
            },
            {
                "group": "COGS",
                "Summary": {
                    "ColData": [
                        {"value": "Total Cost of Goods Sold"},
                        {"value": "105.00"},
                        {"value": ""},
                    ]
                },
                "type": "Section",
            },
        ]
    },
}


class FakeIntuit:
    """Serves the documented shapes; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.token_calls = 0
        self.fail_next_with: int | None = None
        self.expire_access_token = False
        self.cdc: dict[str, Any] | None = None

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, Any]:
        self.requests.append((method, url))
        if url == TOKEN_URL:
            self.token_calls += 1
            assert headers["Authorization"].startswith("Basic ")
            return 200, {
                "access_token": f"access-{self.token_calls}",
                "refresh_token": f"rotated-{self.token_calls}",
                "expires_in": 3600,
            }
        if self.fail_next_with is not None:
            status = self.fail_next_with
            self.fail_next_with = None
            return status, {"Fault": {"type": "throttled" if status == 429 else "auth"}}
        if self.expire_access_token and headers.get("Authorization") == "Bearer access-1":
            return 401, {"Fault": {"type": "AUTHENTICATION"}}
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["minorversion"] == ["75"], "minor version is pinned"
        if "/query" in url:
            query = q["query"][0]
            entity = query.split(" FROM ")[1].split(" ")[0]
            data = {
                "Customer": CUSTOMERS,
                "Item": ITEMS,
                "Invoice": INVOICES,
                "CreditMemo": CREDIT_MEMOS,
            }[entity]
            start = int(query.split("STARTPOSITION ")[1].split(" ")[0])
            size = int(query.split("MAXRESULTS ")[1])
            page = data[start - 1 : start - 1 + size]
            return 200, {"QueryResponse": {entity: page}}
        if "/cdc" in url:
            assert self.cdc is not None, "cdc fixture not armed"
            return 200, self.cdc
        if "/reports/ProfitAndLoss" in url:
            return 200, PNL_REPORT
        if "/companyinfo/" in url:
            return 200, {"CompanyInfo": {"CompanyName": "Sandbox Company_US_1"}}
        return 404, {"Fault": {"type": "not found"}}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("QBO_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("QBO_CLIENT_SECRET", "fake-client-secret-value")
    monkeypatch.setenv("QBO_REALM_ID", "9341453")
    monkeypatch.setenv("QBO_REFRESH_TOKEN", "initial-refresh-token")
    monkeypatch.setenv("QBO_TOKEN_CACHE", str(tmp_path / "token.json"))
    monkeypatch.delenv("QBO_ENVIRONMENT", raising=False)
    return tmp_path


def _connector(fake: FakeIntuit) -> QuickBooksConnector:
    return QuickBooksConnector(CredentialStore(), transport=fake, retry_wait_s=0.0)


def test_full_sync_materializes_engine_readable_files(env: Path) -> None:
    fake = FakeIntuit()
    manifest = run_sync(_connector(fake), env / "state")
    assert manifest["records"]["invoice_lines"]["rows"] == 3
    assert manifest["records"]["customers"]["rows"] == 2
    assert manifest["records"]["gl_totals"]["rows"] == 2
    assert any("cost-unavailable" in w for w in manifest["warnings"])

    sync_dir = SyncStore(env / "state", "quickbooks_online").latest_dir()
    assert sync_dir is not None
    mapping = load_mapping(MAPPING)
    lines = load_lines(sync_dir, mapping)
    assert lines.height == 3

    by_key = {r["source_line_key"]: r for r in lines.to_dicts()}
    sale = by_key["INV-130-1"]
    assert sale["transaction_type"] == "sale"
    assert sale["net_amount_cents"] == 10500
    assert sale["unit_cost_scaled"] == 105000  # 10.50 at scale 10^-4
    credit = by_key["CM-45-1"]
    assert credit["transaction_type"] == "credit"
    assert credit["net_amount_cents"] == -2100, "revenue-effect sign is written"
    service = by_key["INV-130-2"]
    assert service["unit_cost_scaled"] is None, "no purchase cost -> no invented cost"

    gl = load_reference_table(sync_dir, mapping, "gl_totals")
    assert gl is not None


def test_gl_totals_csv_matches_report(env: Path) -> None:
    fake = FakeIntuit()
    run_sync(_connector(fake), env / "state")
    sync_dir = SyncStore(env / "state", "quickbooks_online").latest_dir()
    assert sync_dir is not None
    text = (sync_dir / "gl_totals.csv").read_text(encoding="utf-8")
    assert "2026-09,159.00,105.00" in text
    assert "2026-10,0.00,0" in text  # empty COGS cell -> explicit zero


def test_incremental_cdc_upserts_and_tombstones(env: Path) -> None:
    fake = FakeIntuit()
    run_sync(_connector(fake), env / "state")

    changed_invoice = json.loads(json.dumps(INVOICES[0]))
    changed_invoice["Line"][0]["Amount"] = 110.00
    fake.cdc = {
        "CDCResponse": [
            {
                "QueryResponse": [
                    {"Invoice": [changed_invoice]},
                    {"CreditMemo": [{"Id": "45", "status": "Deleted"}]},
                    {"Customer": []},
                    {"Item": []},
                ]
            }
        ]
    }
    manifest = run_sync(_connector(fake), env / "state")
    cdc_calls = [u for _m, u in fake.requests if "/cdc" in u]
    assert len(cdc_calls) == 1
    changed_since = urllib.parse.parse_qs(urllib.parse.urlparse(cdc_calls[0]).query)[
        "changedSince"
    ][0]
    assert changed_since < manifest["cursor_used"], "re-fetch window applied"

    sync_dir = SyncStore(env / "state", "quickbooks_online").latest_dir()
    assert sync_dir is not None
    lines = load_lines(sync_dir, load_mapping(MAPPING))
    keys = set(lines["source_line_key"].to_list())
    assert "CM-45-1" not in keys, "deleted credit memo's lines removed"
    assert keys == {"INV-130-1", "INV-130-2"}
    updated = lines.filter(lines["source_line_key"] == "INV-130-1")
    assert updated["net_amount_cents"].item() == 11000


def test_pagination_follows_startposition(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qbo, "PAGE_SIZE", 1)
    fake = FakeIntuit()
    manifest = run_sync(_connector(fake), env / "state")
    assert manifest["records"]["customers"]["rows"] == 2
    customer_queries = [
        u for _m, u in fake.requests if "/query" in u and "Customer" in urllib.parse.unquote(u)
    ]
    assert len(customer_queries) == 3, "two full pages plus the short page"


def test_token_rotation_persisted_and_never_in_outputs(env: Path) -> None:
    fake = FakeIntuit()
    run_sync(_connector(fake), env / "state")
    cache = json.loads((env / "token.json").read_text(encoding="utf-8"))
    assert cache["refresh_token"] == "rotated-1"
    everything = []
    for p in sorted((env / "state").rglob("*")):
        if p.is_file():
            everything.append(p.read_bytes().decode("utf-8", errors="replace"))
    blob = "\n".join(everything)
    for secret in ("fake-client-secret-value", "initial-refresh-token", "rotated-1", "access-1"):
        assert secret not in blob

    # The next connector instance refreshes with the ROTATED token (from the
    # cache), not the original environment one.
    fake2 = FakeIntuit()
    connector2 = _connector(fake2)
    assert connector2.check().ok
    assert fake2.token_calls == 1


def test_401_refreshes_once_and_retries(env: Path) -> None:
    fake = FakeIntuit()
    fake.expire_access_token = True
    connector = _connector(fake)
    result = connector.check()
    assert result.ok, result.message
    assert fake.token_calls == 2, "one initial grant, one refresh after the 401"


def test_429_backs_off_and_succeeds(env: Path) -> None:
    fake = FakeIntuit()
    fake.fail_next_with = 429
    connector = _connector(fake)
    result = connector.check()
    assert result.ok, result.message


def test_check_reports_company_and_missing_settings(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeIntuit()
    assert "Sandbox Company_US_1" in _connector(fake).check().message
    monkeypatch.delenv("QBO_CLIENT_SECRET", raising=False)
    result = _connector(FakeIntuit()).check()
    assert not result.ok
    assert "QBO_CLIENT_SECRET" in result.message


def test_profit_and_loss_parser_tolerates_missing_income() -> None:
    warnings: list[str] = []
    rows = parse_profit_and_loss({"Columns": {"Column": []}, "Rows": {"Row": []}}, warnings)
    assert rows == []
    assert warnings and "no month columns" in warnings[0]


def test_environment_validation(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QBO_ENVIRONMENT", "staging")
    with pytest.raises(ConnectorError, match="sandbox"):
        _connector(FakeIntuit()).sync(None)


@pytest.mark.live
def test_live_sandbox_sync_and_engine_run(tmp_path: Path) -> None:
    """Opt-in: a full sync against the owner's Intuit sandbox, then an engine
    ingest of the result. Skips cleanly when credentials are absent."""
    creds = CredentialStore()
    needed = ["QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_REALM_ID"]
    if any(not creds.get(n) for n in needed):
        pytest.skip("QuickBooks sandbox credentials not configured")
    connector = QuickBooksConnector(creds)
    check = connector.check()
    assert check.ok, check.message
    manifest = run_sync(connector, tmp_path / "state")
    assert manifest["records"]["customers"]["rows"] > 0
    sync_dir = SyncStore(tmp_path / "state", "quickbooks_online").latest_dir()
    assert sync_dir is not None
    lines = load_lines(sync_dir, load_mapping(MAPPING))
    assert lines.height > 0
