"""Named filters, sort and pagination - an exact port of the static
adapter's semantics (web/src/lib/api/staticAdapter.ts), so the two data
sources are interchangeable and the parity tests can prove it."""

from __future__ import annotations

from typing import Any

Row = dict[str, Any]

# Nullable guidance-row fields whose nulls the exported file omits.
GUIDANCE_NULLABLE = [
    "segment",
    "customer_name",
    "item_name",
    "current_index",
    "band_p25",
    "band_p50",
    "band_p75",
    "floor_index",
    "pocket_floor_cents",
    "invoice_floor_cents",
    "invoice_floor_gross_cents",
    "invoice_contribution_floor_cents",
    "binding_floor_basis",
    "list_provenance",
    "target_index",
    "target_label",
    "annual_net_revenue_cents",
]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _range(value: float | None, lo: Any, hi: Any) -> bool:
    if value is None:
        return lo is None and hi is None
    if lo is not None and value < float(lo):
        return False
    return not (hi is not None and value > float(hi))


def _list(v: Any) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def _match_guidance(r: Row, f: Row) -> bool:
    ci = _num(r.get("current_index"))
    fi = _num(r.get("floor_index"))
    return (
        (not f.get("customer") or r.get("customer_id") == f["customer"])
        and (not f.get("item") or r.get("item_id") == f["item"])
        and (not _list(f.get("statuses")) or str(r.get("status")) in _list(f.get("statuses")))
        and (
            not _list(f.get("segments"))
            or str(r.get("segment") if r.get("segment") is not None else "")
            in _list(f.get("segments"))
        )
        and _range(ci * 100 if ci is not None else None, f.get("pol_min"), f.get("pol_max"))
        and _range(
            (ci - fi) * 100 if ci is not None and fi is not None else None,
            f.get("gap_min"),
            f.get("gap_max"),
        )
    )


def _match_opportunities(r: Row, f: Row) -> bool:
    pbt = _num(r.get("pct_below_target"))
    gross = _num(r.get("gross_price_opportunity_cents"))
    return (
        (not f.get("customer") or r.get("customer_id") == f["customer"])
        and (not f.get("item") or r.get("item_id") == f["item"])
        and (
            not _list(f.get("evidence"))
            or str(r.get("evidence_status") or "insufficient_data") in _list(f.get("evidence"))
        )
        and (
            not _list(f.get("treatments"))
            or str(r.get("volume_treatment")) in _list(f.get("treatments"))
        )
        and _range(
            gross / 100 if gross is not None else None, f.get("gross_min"), f.get("gross_max")
        )
        and _range(pbt * 100 if pbt is not None else None, f.get("below_min"), f.get("below_max"))
    )


def _match_customers(r: Row, f: Row) -> bool:
    flags = r.get("service_flags") or []
    ret = _num(r.get("returns_contribution_cents"))
    exc = _num(r.get("pct_revenue_excluded"))
    return (
        (not f.get("customer") or r.get("customer_id") == f["customer"])
        and (not _list(f.get("flags")) or any(fl in flags for fl in _list(f.get("flags"))))
        and (not f.get("losing") or (r.get("contribution_after_activity_cents") or 0) < 0)
        and _range(
            ret / 100 if ret is not None else None, f.get("returns_min"), f.get("returns_max")
        )
        and _range(
            exc * 100 if exc is not None else None, f.get("excluded_min"), f.get("excluded_max")
        )
        and _range(_num(r.get("n_orders")), f.get("orders_min"), f.get("orders_max"))
    )


MATCHERS = {
    "guidance": _match_guidance,
    "opportunities": _match_opportunities,
    "customers": _match_customers,
}


def apply_sort(rows: list[Row], sort: str | None) -> list[Row]:
    """Nulls last regardless of direction - same as the static adapter (which
    sorts nulls last ascending, then reverses the WHOLE list for descending,
    putting them first; we reproduce that exactly for parity)."""
    if not sort:
        return rows
    desc = sort.startswith("-")
    field = sort[1:] if desc else sort

    def key(r: Row) -> tuple[int, Any]:
        v = r.get(field)
        return (1, 0) if v is None else (0, v)

    out = sorted(rows, key=key)
    return list(reversed(out)) if desc else out


def paginate(
    rows: list[Row],
    filters: Row,
    sort: str | None,
    page: int,
    page_size: int,
    table: str,
) -> dict[str, Any]:
    match = MATCHERS[table]
    filtered = [r for r in rows if match(r, filters)]
    ordered = apply_sort(filtered, sort)
    page = max(1, page)
    page_size = min(max(1, page_size), 5000)
    start = (page - 1) * page_size
    chunk = ordered[start : start + page_size]
    return {
        "items": chunk,
        "total": len(filtered),
        "total_unfiltered": len(rows),
        "page": page,
        "page_size": page_size,
        "has_more": start + len(chunk) < len(filtered),
    }
