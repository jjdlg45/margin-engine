"""Inventory costing timing (Phase 14, LIFO added in Run 4): receipts-based
COGS under standard, average, FIFO and LIFO methods, with exact integer
arithmetic.

Definitions (documented; fixtures assert them to the cent):
- standard: every unit costs the item's standard cost (items reference or the
  first receipt's unit cost when no standard is mapped; labeled).
- average: moving weighted average. On each sale,
  COGS = half_even(remaining_value x qty_sold / qty_remaining), and the
  remaining value decreases by exactly that COGS — so value conservation
  (receipts value == COGS + ending inventory value) holds exactly.
- fifo: layers consumed in receipt order; COGS is the exact sum of consumed
  layer costs.
- lifo: layers consumed newest-first (periodic-perpetual LIFO on receipt
  layers); COGS is the exact sum of consumed layer costs. Older layers can
  persist indefinitely, so a cost change reaches COGS immediately while new
  receipts keep arriving, but ending inventory carries old costs.

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
    if method not in ("standard", "average", "fifo", "lifo"):
        msg = f"unknown costing method '{method}'"
        raise ValueError(msg)
    # Line-level outputs accumulate into typed column lists, not dicts (Phase
    # 35): at real volumes a dict per sale line is most of this module's RAM.
    out_keys: list[str] = []
    out_items: list[str] = []
    out_cogs: list[int] = []
    out_flags: list[str] = []
    identity_rows: list[dict[str, object]] = []
    r_by_item = {k: v for (k,), v in receipts.group_by(["item_id"])}
    s_by_item = {k: v for (k,), v in sales.group_by(["item_id"])}
    for item, s in sorted(s_by_item.items(), key=lambda kv: str(kv[0])):
        r = r_by_item.get(item)
        # Events carry (date, order, kind, source_line_key, quantity, unit_cost).
        events: list[tuple[object, int, str, str | None, float, int]] = []
        if r is not None:
            for rdate, rqty, rcost in (
                r.sort("receipt_date")
                .select("receipt_date", "quantity", "unit_cost_cents")
                .iter_rows()
            ):
                events.append((rdate, 0, "receipt", None, float(rqty), int(rcost)))
        for sdate, skey, sqty in (
            s.sort("invoice_date", "source_line_key")
            .select("invoice_date", "source_line_key", "quantity")
            .iter_rows()
        ):
            events.append((sdate, 1, "sale", skey, float(sqty), 0))
        events.sort(key=lambda e: (e[0], e[1]))

        layers: deque[tuple[int, int]] = deque()  # (qty, unit_cost_cents)
        qty_on_hand = 0
        value_on_hand = 0
        receipts_value = 0
        cogs_total = 0
        last_cost = 0
        std = (standard_cost or {}).get(str(item))
        for _, _, kind, skey, ev_qty, ev_cost in events:
            if kind == "receipt":
                q = int(ev_qty)
                c = ev_cost
                if std is None:
                    std = c  # standard defaults to the first receipt cost
                layers.append((q, c))
                qty_on_hand += q
                value_on_hand += q * c
                receipts_value += q * c
                last_cost = c
                continue
            q = round(ev_qty)
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
            else:  # fifo / lifo: layers consumed oldest-first / newest-first
                newest_first = method == "lifo"
                cogs = 0
                remaining = q
                while remaining > 0 and layers:
                    lq, lc = layers[-1] if newest_first else layers[0]
                    take = min(lq, remaining)
                    cogs += take * lc
                    remaining -= take
                    if take == lq:
                        if newest_first:
                            layers.pop()
                        else:
                            layers.popleft()
                    elif newest_first:
                        layers[-1] = (lq - take, lc)
                    else:
                        layers[0] = (lq - take, lc)
                    qty_on_hand -= take
                    value_on_hand -= take * lc
                    last_cost = lc
                if remaining > 0:
                    cogs += remaining * last_cost
                    flag = "insufficient_receipts"
            cogs_total += cogs
            out_keys.append(str(skey))
            out_items.append(str(item))
            out_cogs.append(cogs)
            out_flags.append(flag)
        ending = value_on_hand if method in ("average", "fifo", "lifo") else None
        identity_rows.append(
            {
                "item_id": item,
                "method": method,
                "receipts_value_cents": receipts_value,
                "cogs_cents": cogs_total,
                "ending_inventory_cents": ending,
            }
        )
    per_sale = pl.DataFrame(
        {
            "source_line_key": out_keys,
            "item_id": out_items,
            "method": [method] * len(out_keys),
            "cogs_cents": out_cogs,
            "cogs_flag": out_flags,
        },
        schema={
            "source_line_key": pl.String,
            "item_id": pl.String,
            "method": pl.String,
            "cogs_cents": pl.Int64,
            "cogs_flag": pl.String,
        },
    )
    return CostingResult(per_sale=per_sale, identity=pl.DataFrame(identity_rows))


def scenario_timing_statement(method: str, cost_pool: str) -> str:
    """Honest statement of when a cost change reaches COGS for a scenario,
    written as plain-English sentences (it renders verbatim in the UI)."""
    if cost_pool == "existing_pool":
        return (
            "The cost change revalues the existing inventory pool, so it "
            "reaches cost of goods sold (COGS) immediately (a revaluation)."
        )
    base = {
        "fifo": (
            "The cost change applies to new receipts only. It reaches cost of "
            "goods sold (COGS) once the existing FIFO layers are consumed, so "
            "the lag depends on how much inventory cover is on hand."
        ),
        "lifo": (
            "The cost change applies to new receipts only. Under LIFO the "
            "newest layers are consumed first, so it reaches cost of goods "
            "sold (COGS) immediately while new receipts keep arriving; ending "
            "inventory keeps the older, lower-cost layers."
        ),
        "average": (
            "The cost change applies to new receipts only. It starts diluting "
            "into the moving average cost, and so into cost of goods sold "
            "(COGS), immediately with the first new receipt."
        ),
        "standard": (
            "Under standard costing, the change reaches cost of goods sold "
            "(COGS) at the next standard-cost revision. That timing is a "
            "policy decision, not something the data can derive."
        ),
    }[method]
    if cost_pool == "blend":
        return (
            "This scenario blends the existing inventory pool with new "
            "receipts: the existing-pool share reaches cost of goods sold "
            "(COGS) immediately (a revaluation). For the new-receipts share: "
            f"{base}"
        )
    return base
