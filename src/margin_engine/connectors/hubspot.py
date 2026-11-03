"""HubSpot CRM connector (Run 7, Phase 93).

Status: built against the documented HubSpot CRM v3 API, not yet verified
live - live verification needs the owner's developer test account and a
private app token with read-only scopes (see docs/run7-report.md).
Endpoints, limits and terms are recorded in docs/data-sources.md.

Pulls (read-only scopes): companies (customer names) and deals with their
line items and stages. Materializes `customers.csv` and `quotes.csv`, plus
an informational `quote_exclusions.csv`.

Honesty rules, consistent with the engine's quote handling (Run 6, Phase 77):
- only closed-won and closed-lost deals become quote outcomes; open,
  stage-unknown, company-less and line-item-less deals are excluded, each
  with its reason recorded in quote_exclusions.csv and counted in warnings;
- HubSpot line items carry ONE price, so there is no separate close price;
  the mapping documents that, and the negotiation-gap analysis stays off.
- Won/lost comes from the deal pipeline's own stage metadata (isClosed and
  probability), never from guessing stage labels.
- The spec asked for "quotes where available". The quote rows here are
  derived from decided DEALS and their line items; HubSpot's separate Quotes
  object is NOT read in this run, so its scope is not requested either. A
  quote object carries its own states, versions and associations, none of
  which could be verified without a live account - deriving the rows from
  deals is the part that can be built honestly today.

Salesforce note (docs/connectors.md): the same pattern fits a Salesforce
free Developer Edition, but that connector is not built in this run.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from margin_engine.connectors.quickbooks import Transport, decimal_str
from margin_engine.connectors.registry import register

CONNECTOR_VERSION = "1.0"
BASE = "https://api.hubapi.com"
PAGE_SIZE = 100
# docs/data-sources.md: private apps get ~100-190 requests per 10s -> stay
# well under the lowest tier.
MIN_INTERVAL_S = 0.15
REFETCH_WINDOW = timedelta(days=1)
DEAL_PROPERTIES = "dealname,dealstage,pipeline,createdate,closedate,hs_lastmodifieddate"
LINE_ITEM_PROPERTIES = ["quantity", "price", "hs_product_id", "name"]

SETTINGS = (
    SettingSpec(
        env="HUBSPOT_TOKEN",
        purpose="private app token with read-only scopes for companies, deals, "
        "line items and quotes",
    ),
)

EXCL_OPEN = "open"
EXCL_NO_STAGE = "stage_not_recognized"
EXCL_NO_COMPANY = "no_company"
EXCL_NO_LINE_ITEMS = "no_line_items"


def default_transport(limiter: RateLimiter) -> Transport:
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
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if isinstance(payload, dict) and retry_after:
                payload["_retry_after"] = retry_after
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"HubSpot is unreachable ({exc}); the previous sync stands."
            raise ConnectorError(msg) from exc

    return call


class HubSpotConnector(SystemConnector):
    def __init__(
        self,
        credentials: CredentialStore,
        transport: Transport | None = None,
        *,
        retry_wait_s: float = 10.0,
    ) -> None:
        self._creds = credentials
        self._limiter = RateLimiter(min_interval_s=MIN_INTERVAL_S)
        self._transport: Transport = transport or default_transport(self._limiter)
        self._retry_wait_s = retry_wait_s

    def _headers(self) -> dict[str, str]:
        token = self._creds.get("HUBSPOT_TOKEN")
        if not token:
            msg = "HUBSPOT_TOKEN is not set (environment or .env)."
            raise ConnectorError(msg)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{BASE}{path}"
        payload_bytes = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(3):
            status, payload = self._transport(method, url, self._headers(), payload_bytes)
            if status == 200:
                return payload
            if status == 429 and attempt < 2:
                wait = self._retry_wait_s
                if isinstance(payload, dict) and payload.get("_retry_after"):
                    with contextlib.suppress(ValueError):
                        wait = float(payload["_retry_after"])
                time.sleep(wait)
                continue
            if status == 401:
                msg = (
                    "HubSpot rejected the token (401). Check HUBSPOT_TOKEN and the "
                    "private app's read scopes (companies, deals, line items)."
                )
                raise ConnectorError(msg)
            detail = json.dumps(payload)[:300] if payload else str(status)
            msg = f"HubSpot API call failed (HTTP {status}): {detail}"
            raise ConnectorError(msg)
        msg = "HubSpot API call failed after retries (rate limited)."
        raise ConnectorError(msg)

    def _list_all(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            q = dict(params)
            q["limit"] = str(PAGE_SIZE)
            if after:
                q["after"] = after
            data = self._call("GET", f"{path}?{urllib.parse.urlencode(q)}")
            out.extend(data.get("results", []))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                return out

    # -- interface ----------------------------------------------------------

    def describe(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="hubspot",
            version=CONNECTOR_VERSION,
            system="HubSpot CRM",
            description=(
                "Pulls companies (customer names) and deals with line items and "
                "stages through the CRM v3 API (private app token, read-only "
                "scopes); decided deals become quotes with won/lost outcomes."
            ),
            produces=("customers.csv", "quotes.csv", "quote_exclusions.csv"),
            required_settings=SETTINGS,
            # Only what this connector actually reads. The run's rule is the
            # narrowest read-only scopes available, so the HubSpot Quotes
            # object's scope is NOT listed: the quote rows here come from
            # decided deals and their line items (see the module docstring),
            # and a scope the code never uses would misdescribe what the
            # token is for.
            scopes=(
                "crm.objects.companies.read",
                "crm.objects.deals.read",
                "crm.objects.line_items.read",
            ),
            live_verification=(
                "built against the documented CRM v3 API, not yet verified live; "
                "verification needs the owner's HubSpot developer test account"
            ),
            mapping_file=None,  # merged into a main mapping; see docs/connectors.md
        )

    def check(self) -> CheckResult:
        missing = self._creds.missing(self.describe())
        if missing:
            return CheckResult(
                ok=False, message="Missing settings: " + ", ".join(s.env for s in missing)
            )
        try:
            stages = self._stage_classes()
            companies = self._call("GET", "/crm/v3/objects/companies?limit=1")
        except ConnectorError as exc:
            return CheckResult(ok=False, message=str(exc))
        n = int(companies.get("total", 0)) or len(companies.get("results", []))
        return CheckResult(
            ok=True,
            message=(
                f"Connected to HubSpot; {len(stages)} deal stage(s) loaded and "
                f"companies are readable ({n} visible on the first page)."
            ),
        )

    def _stage_classes(self) -> dict[str, str]:
        """Stage id -> won | lost | open, from the pipeline's own metadata."""
        data = self._call("GET", "/crm/v3/pipelines/deals")
        out: dict[str, str] = {}
        for pipeline in data.get("results", []):
            for stage in pipeline.get("stages", []):
                meta = stage.get("metadata", {})
                closed = str(meta.get("isClosed", "")).lower() == "true"
                prob_raw = str(meta.get("probability", ""))
                if not closed:
                    out[str(stage["id"])] = "open"
                    continue
                try:
                    prob = float(prob_raw)
                except ValueError:
                    continue  # closed stage without a probability: unrecognized
                out[str(stage["id"])] = "won" if prob >= 1.0 else "lost"
        return out

    def sync(self, since: str | None) -> SyncBatch:
        warnings: list[str] = []
        now = datetime.now(UTC)
        stage_class = self._stage_classes()

        if since is None:
            companies = self._list_all("/crm/v3/objects/companies", {"properties": "name"})
            deals = self._list_all(
                "/crm/v3/objects/deals",
                {"properties": DEAL_PROPERTIES, "associations": "companies,line_items"},
            )
            archived_deal_ids: list[str] = []
            archived_company_ids: list[str] = []
        else:
            try:
                cursor_dt = datetime.fromisoformat(since)
            except ValueError:
                cursor_dt = now - timedelta(days=3650)
            since_ms = int((cursor_dt - REFETCH_WINDOW).timestamp() * 1000)
            changed_ids = self._search_changed_deal_ids(since_ms)
            deals = [
                self._call(
                    "GET",
                    f"/crm/v3/objects/deals/{deal_id}?"
                    + urllib.parse.urlencode(
                        {"properties": DEAL_PROPERTIES, "associations": "companies,line_items"}
                    ),
                )
                for deal_id in changed_ids
            ]
            companies = self._list_all("/crm/v3/objects/companies", {"properties": "name"})
            archived_deal_ids = [
                str(d["id"])
                for d in self._list_all(
                    "/crm/v3/objects/deals", {"archived": "true", "properties": "dealname"}
                )
            ]
            archived_company_ids = [
                str(c["id"])
                for c in self._list_all(
                    "/crm/v3/objects/companies", {"archived": "true", "properties": "name"}
                )
            ]

        company_rows = [
            {
                "COMPANY_ID": str(c["id"]),
                "COMPANY_NAME": str(c.get("properties", {}).get("name", "") or ""),
            }
            for c in companies
            if not c.get("archived")
        ]

        quote_rows: list[dict[str, str]] = []
        exclusion_rows: list[dict[str, str]] = []
        decided_deal_ids: list[str] = []
        excl_counts: dict[str, int] = {}

        def exclude(deal_id: str, reason: str) -> None:
            exclusion_rows.append({"QUOTE_ID": deal_id, "REASON": reason})
            excl_counts[reason] = excl_counts.get(reason, 0) + 1

        for deal in deals:
            if not deal or deal.get("archived"):
                continue
            deal_id = str(deal["id"])
            props = deal.get("properties", {})
            stage = str(props.get("dealstage", ""))
            outcome = stage_class.get(stage)
            if outcome is None:
                exclude(deal_id, EXCL_NO_STAGE)
                continue
            if outcome == "open":
                exclude(deal_id, EXCL_OPEN)
                continue
            company_id = _first_association(deal, "companies")
            if company_id is None:
                exclude(deal_id, EXCL_NO_COMPANY)
                continue
            line_item_ids = _association_ids(deal, "line items") or _association_ids(
                deal, "line_items"
            )
            if not line_item_ids:
                exclude(deal_id, EXCL_NO_LINE_ITEMS)
                continue
            line_items = self._read_line_items(line_item_ids)
            if not line_items:
                exclude(deal_id, EXCL_NO_LINE_ITEMS)
                continue
            decided_deal_ids.append(deal_id)
            quote_date = str(props.get("createdate", ""))[:10]
            close_date = str(props.get("closedate", "") or "")[:10]
            for li in line_items:
                li_props = li.get("properties", {})
                item_id = str(li_props.get("hs_product_id", "") or f"LI-{li['id']}")
                quote_rows.append(
                    {
                        "LINE_ID": str(li["id"]),
                        "QUOTE_ID": deal_id,
                        "QUOTE_DT": quote_date,
                        "CLOSE_DT": close_date,
                        "CUSTOMER_ID": company_id,
                        "ITEM_ID": item_id,
                        "ITEM_NAME": str(li_props.get("name", "") or ""),
                        "QTY": decimal_str(li_props.get("quantity")),
                        "QUOTE_PRC": decimal_str(li_props.get("price")),
                        "OUTCOME": outcome,
                    }
                )
        if excl_counts:
            parts = ", ".join(f"{n} {reason}" for reason, n in sorted(excl_counts.items()))
            warnings.append(
                f"deals excluded from quotes (only closed-won and closed-lost "
                f"count as outcomes): {parts}; reasons per deal are in "
                f"quote_exclusions.csv"
            )

        quote_deletes = list(dict.fromkeys(archived_deal_ids))
        tables = {
            "customers": TableBatch(
                key=("COMPANY_ID",),
                upserts=_frame(company_rows, {"COMPANY_ID": pl.String, "COMPANY_NAME": pl.String}),
                deletes=(
                    pl.DataFrame({"COMPANY_ID": archived_company_ids})
                    if archived_company_ids
                    else None
                ),
            ),
            "quotes": TableBatch(
                key=("LINE_ID",),
                upserts=_frame(quote_rows, QUOTE_SCHEMA),
                deletes=(pl.DataFrame({"QUOTE_ID": quote_deletes}) if quote_deletes else None),
            ),
            "quote_exclusions": TableBatch(
                key=("QUOTE_ID",),
                upserts=_frame(exclusion_rows, {"QUOTE_ID": pl.String, "REASON": pl.String}),
                # A deal that has since been decided (or archived) stops being
                # an exclusion.
                deletes=(
                    pl.DataFrame({"QUOTE_ID": decided_deal_ids + quote_deletes})
                    if decided_deal_ids or quote_deletes
                    else None
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

    def _search_changed_deal_ids(self, since_ms: int) -> list[str]:
        ids: list[str] = []
        after: str | None = None
        while True:
            body: dict[str, Any] = {
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "hs_lastmodifieddate",
                                "operator": "GTE",
                                "value": str(since_ms),
                            }
                        ]
                    }
                ],
                "properties": ["hs_lastmodifieddate"],
                "limit": PAGE_SIZE,
            }
            if after:
                body["after"] = after
            data = self._call("POST", "/crm/v3/objects/deals/search", body)
            ids.extend(str(r["id"]) for r in data.get("results", []))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                return ids

    def _read_line_items(self, ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), PAGE_SIZE):
            chunk = ids[start : start + PAGE_SIZE]
            data = self._call(
                "POST",
                "/crm/v3/objects/line_items/batch/read",
                {
                    "properties": LINE_ITEM_PROPERTIES,
                    "inputs": [{"id": i} for i in chunk],
                },
            )
            out.extend(data.get("results", []))
        return out


QUOTE_SCHEMA: dict[str, Any] = {
    "LINE_ID": pl.String,
    "QUOTE_ID": pl.String,
    "QUOTE_DT": pl.String,
    "CLOSE_DT": pl.String,
    "CUSTOMER_ID": pl.String,
    "ITEM_ID": pl.String,
    "ITEM_NAME": pl.String,
    "QTY": pl.String,
    "QUOTE_PRC": pl.String,
    "OUTCOME": pl.String,
}


def _frame(rows: list[dict[str, str]], schema: dict[str, Any]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows).select(list(schema))


def _association_ids(deal: dict[str, Any], key: str) -> list[str]:
    block = deal.get("associations", {}).get(key, {})
    return [str(r["id"]) for r in block.get("results", [])]


def _first_association(deal: dict[str, Any], key: str) -> str | None:
    ids = _association_ids(deal, key)
    return ids[0] if ids else None


register("hubspot", lambda creds: HubSpotConnector(creds))
