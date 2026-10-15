"""Inventory costing timing (Phase 14): receipts-based COGS under standard,
average and FIFO methods, with exact integer arithmetic.

Definitions (documented; fixtures assert them to the cent):
- standard: every unit costs the item's standard cost (items reference or the
  first receipt's unit cost when no standard is mapped; labeled).
- average: moving weighted average. On each sale,
  COGS = half_even(remaining_value x qty_sold / qty_remaining), and the
  remaining value decreases by exactly that COGS — so value conservation
  (receipts value == COGS + ending inventory value) holds exactly.
- fifo: layers consumed in receipt order; COGS is the exact sum of consumed
  layer costs.

Sales beyond available receipt quantity are costed at the last known unit cost
and flagged (`cogs_flag = insufficient_receipts`) — never silently invented.

Timing statements for tariff scenarios: under FIFO a cost change reaches COGS
after existing layers are consumed (months estimated from current inventory /
recent demand); under average it dilutes in immediately; under standard it
waits for the next standard-cost revision (policy, stated not guessed).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import polars as pl


@dataclass
class CostingResult:
    per_sale: pl.DataFrame  # source_line_key, method, cogs_cents, cogs_flag
    identity: pl.DataFrame  # per item: receipts value == cogs + ending inventory


def _half_even(numer: int, denom: int) -> int:
    q, r = divmod(numer, denom)
    if 2 * r > denom or (2 * r == denom and q % 2 != 0):
        q += 1
    return q


def cost_sales(
    receipts: pl.DataFrame,
    sales: pl.DataFrame,
    method: str,
    standard_cost: dict[str, int] | None = None,
) -> CostingResult:
    """receipts: item_id, receipt_date, quantity, unit_cost_cents.
    sales: source_line_key, item_id, invoice_date, quantity (units, positive).
    All events processed in date order (receipts before sales on the same day).
    """
    if method not in ("standard", "average", "fifo"):
        msg = f"unknown costing method '{method}'"
        raise ValueError(msg)
    rows: list[dict[str, object]] = []
    identity_rows: list[dict[str, object]] = []
    r_by_item = {k: v for (k,), v in receipts.group_by(["item_id"])}
    s_by_item = {k: v for (k,), v in sales.group_by(["item_id"])}
    for item, s in sorted(s_by_item.items(), key=lambda kv: str(kv[0])):
        r = r_by_item.get(item)
        events: list[tuple[object, int, str, dict[str, object]]] = []
        if r is not None:
            for row in r.sort("receipt_date").to_dicts():
                events.append((row["receipt_date"], 0, "receipt", row))
        for row in s.sort("invoice_date", "source_line_key").to_dicts():
            events.append((row["invoice_date"], 1, "sale", row))
        events.sort(key=lambda e: (e[0], e[1]))

        layers: deque[tuple[int, int]] = deque()  # (qty, unit_cost_cents)
        qty_on_hand = 0
        value_on_hand = 0
        receipts_value = 0
        cogs_total = 0
        last_cost = 0
        std = (standard_cost or {}).get(str(item))
        for _, _, kind, row in events:
            if kind == "receipt":
                q = int(row["quantity"])  # type: ignore[call-overload]
                c = int(row["unit_cost_cents"])  # type: ignore[call-overload]
                if std is None:
                    std = c  # standard defaults to the first receipt cost
                layers.append((q, c))
                qty_on_hand += q
                value_on_hand += q * c
                receipts_value += q * c
                last_cost = c
                continue
            q = round(float(row["quantity"]))  # type: ignore[arg-type]
            flag = ""
            if method == "standard":
                cogs = q * int(std if std is not None else last_cost)
                if std is None:
                    flag = "no_standard_cost"
            elif method == "average":
                if qty_on_hand <= 0:
                    cogs = q * last_cost
                    flag = "insufficient_receipts"
                else:
                    take = min(q, qty_on_hand)
                    cogs = _half_even(value_on_hand * take, qty_on_hand)
                    short = q - take
                    if short > 0:
                        cogs += short * last_cost
                        flag = "insufficient_receipts"
                    value_on_hand -= _half_even(value_on_hand * take, qty_on_hand)
                    qty_on_hand -= take
            else:  # fifo
                cogs = 0
                remaining = q
                while remaining > 0 and layers:
                    lq, lc = layers[0]
                    take = min(lq, remaining)
                    cogs += take * lc
                    remaining -= take
                    if take == lq:
                        layers.popleft()
                    else:
                        layers[0] = (lq - take, lc)
                    qty_on_hand -= take
                    value_on_hand -= take * lc
                    last_cost = lc
                if remaining > 0:
                    cogs += remaining * last_cost
                    flag = "insufficient_receipts"
            cogs_total += cogs
            rows.append(
                {
                    "source_line_key": row["source_line_key"],
                    "item_id": item,
                    "method": method,
                    "cogs_cents": cogs,
                    "cogs_flag": flag,
                }
            )
        ending = value_on_hand if method in ("average", "fifo") else None
        identity_rows.append(
            {
                "item_id": item,
                "method": method,
                "receipts_value_cents": receipts_value,
                "cogs_cents": cogs_total,
                "ending_inventory_cents": ending,
            }
        )
    return CostingResult(per_sale=pl.DataFrame(rows), identity=pl.DataFrame(identity_rows))


def scenario_timing_statement(method: str, cost_pool: str) -> str:
    """Honest statement of when a cost change reaches COGS for a scenario."""
    if cost_pool == "existing_pool":
        return "cost change applied to the existing pool: reaches COGS immediately (revaluation)"
    base = {
        "fifo": (
            "new receipts only: reaches COGS after existing FIFO layers are "
            "consumed (lag depends on inventory cover)"
        ),
        "average": "new receipts only: dilutes into moving average immediately on first receipt",
        "standard": (
            "standard costing: reaches COGS at the next standard-cost revision "
            "(a policy decision, not derivable from the data)"
        ),
    }[method]
    if cost_pool == "blend":
        return f"blend of existing pool and new receipts; for new receipts: {base}"
    return base
