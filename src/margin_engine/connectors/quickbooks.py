"""QuickBooks Online connector (Run 7, Phase 92).

Status: built against the documented Intuit Accounting API (minorversion 75,
the only supported minor version since 2025-08-01), not yet verified live -
live verification needs the owner's Intuit developer app and sandbox company
(see docs/run7-report.md). Endpoints, limits and terms are recorded in
docs/data-sources.md.

Pulls (read-only accounting scope): customers, items, invoices and their
lines, credit memos, and a monthly profit-and-loss report for the GL
variance check. Materializes `invoice_lines.csv`, `customers.csv`,
`items.csv` and `gl_totals.csv`; the committed mapping
`config/mappings/quickbooks_online.yaml` reads them.

Honest data gaps:
- QuickBooks invoice lines carry no unit cost. The connector joins the
  item's purchase cost where the item declares one; lines without it get a
  blank cost and the engine reports them `cost_unavailable`. A current
  purchase cost is not a transaction-time cost; the mapping says so.
- QuickBooks stores credit-memo lines positive. The connector writes the
  revenue-effect sign (negative amount and quantity) so the mapping's
  `sign_convention: natural` holds; the mapping documents this.

Incremental sync uses the Change Data Capture endpoint with a one-day
re-fetch window; deleted documents arrive as tombstones and remove their
lines. Money values are parsed with Decimal (json parse_float), never float.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from margin_engine.connectors.base import ConnectorError, RateLimiter
from margin_engine.connectors.credentials import CredentialStore
from margin_engine.connectors.framework import (
    CheckResult,
    ConnectorInfo,
    SettingSpec,
    SyncBatch,
    SystemConnector,
    TableBatch,
)
from margin_engine.connectors.registry import register

CONNECTOR_VERSION = "1.0"
MINOR_VERSION = "75"  # the only supported minor version since 2025-08-01
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
BASES = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
SCOPE = "com.intuit.quickbooks.accounting"
PAGE_SIZE = 1000
# docs/data-sources.md: 500 requests/min per realm -> stay well under it.
MIN_INTERVAL_S = 0.2
REFETCH_WINDOW = timedelta(days=1)

# transport(method, url, headers, body) -> (status, parsed json). Injectable
# so tests run offline against documented-schema fixtures.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any]]

SETTINGS = (
    SettingSpec(env="QBO_CLIENT_ID", purpose="Intuit app client ID"),
    SettingSpec(env="QBO_CLIENT_SECRET", purpose="Intuit app client secret"),
    SettingSpec(env="QBO_REALM_ID", purpose="company (realm) ID", secret=False),
    SettingSpec(
        env="QBO_REFRESH_TOKEN",
        purpose="OAuth refresh token (obtained once via `margin-engine connect "
        "quickbooks_online --authorize` or Intuit's OAuth playground); after "
        "the first sync the rotated token lives in the token cache",
        optional=True,
    ),
    SettingSpec(
        env="QBO_ENVIRONMENT",
        purpose="sandbox (default) or production",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="QBO_REDIRECT_URI",
        purpose="OAuth redirect URI registered on the app "
        "(default http://localhost:8723/callback, for --authorize)",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="QBO_TOKEN_CACHE",
        purpose="file holding the rotated refresh token "
        "(default .api_state/quickbooks_token.json; gitignored, never committed)",
        secret=False,
        optional=True,
    ),
    SettingSpec(
        env="QBO_GL_MONTHS",
        purpose="months of profit-and-loss history for the GL variance check (default 24)",
        secret=False,
        optional=True,
    ),
)


def decimal_str(value: Any) -> str:
    """Render a JSON number exactly. The real transport parses floats as
    Decimal; a fixture may still carry int/float."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return str(Decimal(str(value)))
    return str(value)


def default_transport(limiter: RateLimiter) -> Transport:
    """urllib transport with rate limiting; JSON numbers become Decimal."""

    def call(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, Any]:
        limiter.wait()
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8")
                return resp.status, json.loads(text, parse_float=Decimal) if text else {}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(text, parse_float=Decimal)
            except json.JSONDecodeError:
                payload = {"raw": text[:500]}
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"QuickBooks Online is unreachable ({exc}); the previous sync stands."
            raise ConnectorError(msg) from exc

    return call


class _Tokens:
    """Access-token refresh with rotated-refresh-token persistence. Intuit
    rotates the refresh token periodically; losing the rotation means
    re-authorizing, so the newest one is always written to the cache file
    (outside the repo, never in any output)."""

    def __init__(self, creds: CredentialStore, transport: Transport, cache_path: Path) -> None:
        self._creds = creds
        self._transport = transport
        self._cache_path = cache_path
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _read_cache(self) -> dict[str, str]:
        if self._cache_path.exists():
            return dict(json.loads(self._cache_path.read_text(encoding="utf-8")))
        return {}

    def _refresh_token(self) -> str:
        cached = self._read_cache().get("refresh_token")
        token = cached or self._creds.get("QBO_REFRESH_TOKEN")
        if not token:
            msg = (
                "QuickBooks needs a refresh token: run `margin-engine connect "
                "quickbooks_online --authorize` once, or set QBO_REFRESH_TOKEN "
                "from Intuit's OAuth 2.0 playground."
            )
            raise ConnectorError(msg)
        return token

    def access_token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        return self.refresh()

    def refresh(self) -> str:
        client_id = self._creds.get("QBO_CLIENT_ID")
        client_secret = self._creds.get("QBO_CLIENT_SECRET")
        if not client_id or not client_secret:
            msg = "QBO_CLIENT_ID and QBO_CLIENT_SECRET are not set (environment or .env)."
            raise ConnectorError(msg)
        body = urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": self._refresh_token()}
        ).encode("ascii")
        status, payload = self._transport("POST", TOKEN_URL, self._auth_headers(), body)
        if status != 200 or "access_token" not in payload:
            detail = payload.get("error", payload.get("raw", status)) if payload else status
            msg = (
                f"QuickBooks token refresh failed ({detail}). The refresh token "
                f"may have expired (Intuit expires them after ~100 days of "
                f"disuse); re-run `margin-engine connect quickbooks_online "
                f"--authorize`."
            )
            raise ConnectorError(msg)
        self.store_grant(payload)
        return str(payload["access_token"])

    def exchange_code(self, code: str, redirect_uri: str) -> None:
        body = urllib.parse.urlencode(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        ).encode("ascii")
        status, payload = self._transport("POST", TOKEN_URL, self._auth_headers(), body)
        if status != 200 or "refresh_token" not in payload:
            detail = payload.get("error", status) if payload else status
            msg = f"QuickBooks authorization-code exchange failed ({detail})."
            raise ConnectorError(msg)
        self.store_grant(payload)

    def store_grant(self, payload: dict[str, Any]) -> None:
        self._access_token = str(payload["access_token"]) if "access_token" in payload else None
        expires_in = float(payload.get("expires_in", 3600))
        self._expires_at = time.monotonic() + expires_in
        rotated = payload.get("refresh_token")
        if rotated:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(
                    {
                        "refresh_token": str(rotated),
                        "rotated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                ),
                encoding="utf-8",
            )

    def _auth_headers(self) -> dict[str, str]:
        client_id = self._creds.get("QBO_CLIENT_ID") or ""
        client_secret = self._creds.get("QBO_CLIENT_SECRET") or ""
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
        return {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }


class QuickBooksConnector(SystemConnector):
    def __init__(
        self,
        credentials: CredentialStore,
        transport: Transport | None = None,
        *,
        retry_wait_s: float = 60.0,
    ) -> None:
        self._creds = credentials
        self._limiter = RateLimiter(min_interval_s=MIN_INTERVAL_S)
        self._transport: Transport = transport or default_transport(self._limiter)
        self._retry_wait_s = retry_wait_s
        self._tokens = _Tokens(credentials, self._transport, self._token_cache_path())

    # -- settings ---------------------------------------------------------

    def _token_cache_path(self) -> Path:
        return Path(self._creds.get("QBO_TOKEN_CACHE") or ".api_state/quickbooks_token.json")

    def _base(self) -> str:
        env = (self._creds.get("QBO_ENVIRONMENT") or "sandbox").strip().lower()
        if env not in BASES:
            msg = f"QBO_ENVIRONMENT must be 'sandbox' or 'production', got '{env}'"
            raise ConnectorError(msg)
        return BASES[env]

    def _realm(self) -> str:
        realm = self._creds.get("QBO_REALM_ID")
        if not realm:
            msg = "QBO_REALM_ID is not set (the company ID shown with the sandbox)."
            raise ConnectorError(msg)
        return realm

    # -- HTTP -------------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> Any:
        url = f"{self._base()}{path}?" + urllib.parse.urlencode(
            {**params, "minorversion": MINOR_VERSION}
        )
        for attempt in range(3):
            headers = {
                "Authorization": f"Bearer {self._tokens.access_token()}",
                "Accept": "application/json",
            }
            status, payload = self._transport("GET", url, headers, None)
            if status == 200:
                return payload
            if status == 401 and attempt == 0:
                self._tokens.refresh()
                continue
            if status == 429 and attempt < 2:
                # docs/data-sources.md: Intuit advises waiting 60s on 429.
                time.sleep(self._retry_wait_s)
                continue
            fault = json.dumps(payload)[:300] if payload else str(status)
            msg = f"QuickBooks API call failed (HTTP {status}): {fault}"
            raise ConnectorError(msg)
        msg = "QuickBooks API call failed after retries."
        raise ConnectorError(msg)

    def _query_all(self, entity: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 1
        while True:
            q = f"SELECT * FROM {entity} ORDERBY Id STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"
            data = self._get(f"/v3/company/{self._realm()}/query", {"query": q})
            page = data.get("QueryResponse", {}).get(entity, [])
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            start += PAGE_SIZE

    def _cdc(self, since: datetime) -> dict[str, list[dict[str, Any]]]:
        data = self._get(
            f"/v3/company/{self._realm()}/cdc",
            {
                "entities": "Customer,Item,Invoice,CreditMemo",
                "changedSince": since.isoformat(timespec="seconds"),
            },
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for block in data.get("CDCResponse", []):
            for qr in block.get("QueryResponse", []):
                for entity, items in qr.items():
                    if isinstance(items, list):
                        out.setdefault(entity, []).extend(items)
        return out

    # -- interface --------------------------------------------------------

    def describe(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="quickbooks_online",
            version=CONNECTOR_VERSION,
            system="QuickBooks Online",
            description=(
                "Pulls customers, items, invoices, credit memos and monthly "
                "profit-and-loss totals through Intuit's Accounting API "
                "(read-only scope, OAuth 2.0 with refresh tokens)."
            ),
            produces=("invoice_lines.csv", "customers.csv", "items.csv", "gl_totals.csv"),
            required_settings=SETTINGS,
            scopes=(SCOPE + " (read-only use: queries and reports only)",),
            live_verification=(
                "built against the documented API (minorversion 75), not yet "
                "verified live; verification needs the owner's Intuit sandbox"
            ),
            mapping_file="config/mappings/quickbooks_online.yaml",
        )

    def check(self) -> CheckResult:
        missing = self._creds.missing(self.describe())
        if missing:
            return CheckResult(
                ok=False,
                message="Missing settings: " + ", ".join(s.env for s in missing),
            )
        try:
            realm = self._realm()
            data = self._get(f"/v3/company/{realm}/companyinfo/{realm}", {})
        except ConnectorError as exc:
            return CheckResult(ok=False, message=str(exc))
        name = data.get("CompanyInfo", {}).get("CompanyName", "unknown company")
        env = (self._creds.get("QBO_ENVIRONMENT") or "sandbox").lower()
        return CheckResult(ok=True, message=f"Connected to '{name}' ({env}).")

    def sync(self, since: str | None) -> SyncBatch:
        warnings: list[str] = []
        now = datetime.now(UTC)
        if since is None:
            customers = self._query_all("Customer")
            items = self._query_all("Item")
            invoices = self._query_all("Invoice")
            credit_memos = self._query_all("CreditMemo")
            deleted_txn_keys: list[str] = []
            deleted_customers: list[str] = []
            deleted_items: list[str] = []
        else:
            try:
                cursor_dt = datetime.fromisoformat(since)
            except ValueError:
                cursor_dt = now - timedelta(days=3650)
            changed = self._cdc(cursor_dt - REFETCH_WINDOW)
            customers = [c for c in changed.get("Customer", []) if c.get("status") != "Deleted"]
            # Items are always fetched in full: changed invoice lines need the
            # purchase-cost map even when no item changed, and the item list
            # is small next to the transaction volume.
            items = self._query_all("Item")
            invoices = [i for i in changed.get("Invoice", []) if i.get("status") != "Deleted"]
            credit_memos = [
                c for c in changed.get("CreditMemo", []) if c.get("status") != "Deleted"
            ]
            deleted_txn_keys = [
                f"INV-{i['Id']}" for i in changed.get("Invoice", []) if i.get("status") == "Deleted"
            ] + [
                f"CM-{c['Id']}"
                for c in changed.get("CreditMemo", [])
                if c.get("status") == "Deleted"
            ]
            deleted_customers = [
                c["Id"] for c in changed.get("Customer", []) if c.get("status") == "Deleted"
            ]
            deleted_items = [
                i["Id"] for i in changed.get("Item", []) if i.get("status") == "Deleted"
            ]

        purchase_cost = {
            str(i["Id"]): decimal_str(i.get("PurchaseCost"))
            for i in items
            if i.get("PurchaseCost") is not None
        }
        line_rows: list[dict[str, str]] = []
        costless = 0
        for txn, prefix, sign in [(i, "INV", 1) for i in invoices] + [
            (c, "CM", -1) for c in credit_memos
        ]:
            txn_id = str(txn["Id"])
            txn_key = f"{prefix}-{txn_id}"
            txn_date = str(txn.get("TxnDate", ""))
            customer = str(txn.get("CustomerRef", {}).get("value", ""))
            currency = str(txn.get("CurrencyRef", {}).get("value", "USD"))
            doc_no = str(txn.get("DocNumber", ""))
            for idx, line in enumerate(txn.get("Line", []), start=1):
                if line.get("DetailType") != "SalesItemLineDetail":
                    continue
                detail = line.get("SalesItemLineDetail", {})
                item_id = str(detail.get("ItemRef", {}).get("value", ""))
                qty = detail.get("Qty")
                unit_price = detail.get("UnitPrice")
                amount = line.get("Amount")
                cost = purchase_cost.get(item_id, "")
                if not cost:
                    costless += 1
                line_rows.append(
                    {
                        "LINE_ID": f"{txn_key}-{line.get('Id', idx)}",
                        "TXN_KEY": txn_key,
                        "TXN_TYPE": prefix,
                        "DOC_NO": doc_no,
                        "TXN_DT": txn_date,
                        "CUSTOMER_ID": customer,
                        "ITEM_ID": item_id,
                        # QuickBooks stores credit-memo lines positive; the
                        # revenue-effect sign is written here so the mapping's
                        # natural sign convention holds (module docstring).
                        "QTY": _signed(qty, sign),
                        "UNIT_PRICE": decimal_str(unit_price),
                        "NET_AMOUNT": _signed(amount, sign),
                        "UNIT_COST": cost,
                        "CURRENCY": currency,
                    }
                )
        if costless:
            warnings.append(
                f"{costless} line(s) reference items without a purchase cost; "
                f"they carry no cost and the engine reports them as "
                f"cost-unavailable rather than inventing one (QuickBooks "
                f"invoice lines have no transaction cost)."
            )

        customer_rows = [
            {"CUSTOMER_ID": str(c["Id"]), "CUSTOMER_NAME": str(c.get("DisplayName", ""))}
            for c in customers
        ]
        item_rows = [
            {
                "ITEM_ID": str(i["Id"]),
                "ITEM_NAME": str(i.get("Name", "")),
                "ITEM_TYPE": str(i.get("Type", "")),
                "PURCHASE_COST": decimal_str(i.get("PurchaseCost")),
            }
            for i in items
        ]
        gl_rows = self._profit_and_loss_months(now, warnings)

        tables = {
            "invoice_lines": TableBatch(
                key=("LINE_ID",),
                upserts=_frame(line_rows, LINE_SCHEMA),
                deletes=(pl.DataFrame({"TXN_KEY": deleted_txn_keys}) if deleted_txn_keys else None),
            ),
            "customers": TableBatch(
                key=("CUSTOMER_ID",),
                upserts=_frame(
                    customer_rows, {"CUSTOMER_ID": pl.String, "CUSTOMER_NAME": pl.String}
                ),
                deletes=(
                    pl.DataFrame({"CUSTOMER_ID": deleted_customers}) if deleted_customers else None
                ),
            ),
            "items": TableBatch(
                key=("ITEM_ID",),
                upserts=_frame(
                    item_rows,
                    {
                        "ITEM_ID": pl.String,
                        "ITEM_NAME": pl.String,
                        "ITEM_TYPE": pl.String,
                        "PURCHASE_COST": pl.String,
                    },
                ),
                deletes=pl.DataFrame({"ITEM_ID": deleted_items}) if deleted_items else None,
            ),
            "gl_totals": TableBatch(
                key=("MONTH",),
                upserts=_frame(
                    gl_rows, {"MONTH": pl.String, "NET_SALES": pl.String, "COGS": pl.String}
                ),
            ),
        }
        cursor = now.isoformat(timespec="seconds")
        return SyncBatch(
            tables=tables,
            cursor=cursor,
            window_start=since,
            window_end=cursor,
            warnings=warnings,
        )

    def _profit_and_loss_months(self, now: datetime, warnings: list[str]) -> list[dict[str, str]]:
        months_raw = self._creds.get("QBO_GL_MONTHS") or "24"
        try:
            months = max(int(months_raw), 1)
        except ValueError:
            months = 24
        end = now.date()
        start_month = (end.year * 12 + end.month - 1) - (months - 1)
        start = date(start_month // 12, start_month % 12 + 1, 1)
        try:
            report = self._get(
                f"/v3/company/{self._realm()}/reports/ProfitAndLoss",
                {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "summarize_column_by": "Month",
                    "accounting_method": "Accrual",
                },
            )
        except ConnectorError as exc:
            warnings.append(
                f"profit-and-loss report unavailable ({exc}); gl_totals.csv was "
                f"not refreshed this sync"
            )
            return []
        return parse_profit_and_loss(report, warnings)

    # -- one-time authorization -------------------------------------------

    def authorize(self) -> str:
        """One-time OAuth consent: opens a local listener on the registered
        redirect URI, prints the consent URL, exchanges the code, and stores
        the refresh token in the token cache. Returns a status message."""
        import secrets as pysecrets
        from http.server import BaseHTTPRequestHandler, HTTPServer

        client_id = self._creds.get("QBO_CLIENT_ID")
        if not client_id:
            msg = "QBO_CLIENT_ID is not set."
            raise ConnectorError(msg)
        redirect = self._creds.get("QBO_REDIRECT_URI") or "http://localhost:8723/callback"
        parsed = urllib.parse.urlparse(redirect)
        if parsed.hostname not in ("localhost", "127.0.0.1"):
            msg = (
                f"--authorize needs a localhost redirect URI (got {redirect}); "
                f"register http://localhost:8723/callback on the Intuit app, or "
                f"use Intuit's OAuth playground and set QBO_REFRESH_TOKEN."
            )
            raise ConnectorError(msg)
        state = pysecrets.token_urlsafe(16)
        consent = (
            AUTH_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "response_type": "code",
                    "scope": SCOPE,
                    "redirect_uri": redirect,
                    "state": state,
                }
            )
        )
        result: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result["code"] = q.get("code", [""])[0]
                result["state"] = q.get("state", [""])[0]
                result["realmId"] = q.get("realmId", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Authorized. You can close this tab.")

            def log_message(self, fmt: str, *args: object) -> None:
                return None

        print(f"Open this URL in a browser and approve access:\n\n  {consent}\n")
        server = HTTPServer((parsed.hostname or "localhost", parsed.port or 80), Handler)
        server.timeout = 300
        server.handle_request()
        server.server_close()
        if not result.get("code"):
            msg = "No authorization code arrived within 5 minutes."
            raise ConnectorError(msg)
        if result.get("state") != state:
            msg = "OAuth state mismatch; aborting (possible cross-site request)."
            raise ConnectorError(msg)
        self._tokens.exchange_code(result["code"], redirect)
        realm_note = ""
        if result.get("realmId"):
            realm_note = f" Set QBO_REALM_ID={result['realmId']} if it is not set already."
        return f"Refresh token stored in {self._token_cache_path()}.{realm_note}"


LINE_SCHEMA: dict[str, type[pl.DataType] | pl.DataType] = {
    "LINE_ID": pl.String,
    "TXN_KEY": pl.String,
    "TXN_TYPE": pl.String,
    "DOC_NO": pl.String,
    "TXN_DT": pl.String,
    "CUSTOMER_ID": pl.String,
    "ITEM_ID": pl.String,
    "QTY": pl.String,
    "UNIT_PRICE": pl.String,
    "NET_AMOUNT": pl.String,
    "UNIT_COST": pl.String,
    "CURRENCY": pl.String,
}


def _frame(rows: list[dict[str, str]], schema: dict[str, Any]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows).select(list(schema))


def _signed(value: Any, sign: int) -> str:
    text = decimal_str(value)
    if not text:
        return text
    amount = Decimal(text) * sign
    return str(amount)


def parse_profit_and_loss(report: Any, warnings: list[str]) -> list[dict[str, str]]:
    """Pull monthly Total Income and Total COGS out of the ProfitAndLoss
    report JSON (documented structure: Columns with month metadata; row
    groups 'Income' and 'COGS' with summary rows)."""
    columns = report.get("Columns", {}).get("Column", [])
    months: list[tuple[int, str]] = []
    for idx, col in enumerate(columns):
        meta = {m.get("Name"): m.get("Value") for m in col.get("MetaData", [])}
        start = meta.get("StartDate")
        if start:
            months.append((idx, str(start)[:7]))
    if not months:
        warnings.append(
            "profit-and-loss report carried no month columns; gl_totals.csv "
            "was not refreshed this sync"
        )
        return []

    totals: dict[str, list[str]] = {}

    def walk(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            group = row.get("group")
            summary = row.get("Summary", {}).get("ColData", [])
            if group in ("Income", "COGS") and summary:
                totals[str(group)] = [str(c.get("value", "")) for c in summary]
            nested = row.get("Rows", {}).get("Row", [])
            if nested:
                walk(nested)

    walk(report.get("Rows", {}).get("Row", []))
    income = totals.get("Income")
    cogs = totals.get("COGS")
    if income is None:
        warnings.append(
            "profit-and-loss report had no Income summary row; gl_totals.csv "
            "was not refreshed this sync"
        )
        return []
    out = []
    for idx, month in months:
        if idx >= len(income):
            continue
        net_sales = income[idx] or "0"
        cogs_v = cogs[idx] if cogs is not None and idx < len(cogs) else "0"
        out.append({"MONTH": month, "NET_SALES": net_sales, "COGS": cogs_v or "0"})
    return out


register("quickbooks_online", lambda creds: QuickBooksConnector(creds))
