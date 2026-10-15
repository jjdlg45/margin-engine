"""Reconciliation: every dollar ties back to source totals (spec principle 3).

Checks are exact (integer cents). Any failed check raises — reconciliation is
never a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl


class ReconciliationError(AssertionError):
    """A dollar went missing or was double counted."""


@dataclass
class ReconciliationReport:
    checks: list[dict[str, Any]] = field(default_factory=list)
    residuals: dict[str, int] = field(default_factory=dict)

    def add(self, name: str, expected_cents: int, actual_cents: int) -> None:
        ok = expected_cents == actual_cents
        self.checks.append(
            {
                "check": name,
                "expected_cents": expected_cents,
                "actual_cents": actual_cents,
                "diff_cents": actual_cents - expected_cents,
                "pass": ok,
            }
        )
        if not ok:
            msg = (
                f"reconciliation failed: {name}: expected {expected_cents} cents, "
                f"got {actual_cents} cents (diff {actual_cents - expected_cents})"
            )
            raise ReconciliationError(msg)

    @property
    def all_pass(self) -> bool:
        return all(c["pass"] for c in self.checks)

    def to_frame(self) -> pl.DataFrame:
        return pl.DataFrame(self.checks)


def reconcile(
    waterfall_lines: pl.DataFrame,
    removed_duplicates: pl.DataFrame,
    source_totals: dict[str, int],
    residuals: dict[str, int],
    *,
    order_charges: pl.DataFrame | None = None,
    freight_log: pl.DataFrame | None = None,
    rebate_settlements: pl.DataFrame | None = None,
    return_freight_log: pl.DataFrame | None = None,
) -> ReconciliationReport:
    rep = ReconciliationReport(residuals=dict(residuals))
    wf = waterfall_lines

    # 1. Row conservation: ledger rows + removed verified duplicates == source rows.
    rep.add(
        "row_conservation",
        source_totals["rows"],
        wf.height + removed_duplicates.height,
    )
    # 2. Net amount conservation to the cent.
    rep.add(
        "net_amount_conservation",
        source_totals["net_amount_cents"],
        int(wf["net_amount_cents"].fill_null(0).sum())
        + int(
            removed_duplicates["net_amount_cents"].fill_null(0).sum()
            if removed_duplicates.height
            else 0
        ),
    )
    # 3. Billed charges: allocated + residual == source table total.
    if order_charges is not None:
        rep.add(
            "billed_charges_allocation",
            int(order_charges["amount_cents"].sum()),
            int(wf["billed_charge_alloc_cents"].fill_null(0).sum())
            + residuals.get("billed_charges_unallocated_cents", 0),
        )
    # 4. Settlement rebates: allocated + residual == settlement table total.
    if rebate_settlements is not None:
        rep.add(
            "settlement_rebate_allocation",
            int(rebate_settlements["amount_cents"].sum()),
            int(wf["rebate_settlement_alloc_cents"].fill_null(0).sum())
            + residuals.get("settlement_rebates_unallocated_cents", 0),
        )
    # 5. Freight expense: allocated + residual == freight log total.
    if freight_log is not None:
        rep.add(
            "freight_expense_allocation",
            int(freight_log["amount_cents"].sum()),
            int(wf["freight_expense_alloc_cents"].fill_null(0).sum())
            + residuals.get("freight_expense_unallocated_cents", 0),
        )
    # 5b. Return freight expense: actual allocations + residual == log total.
    if return_freight_log is not None:
        actual_alloc = int(
            wf.filter(
                (pl.col("transaction_type") == "return")
                & (pl.col("return_freight_exp_status") == "actual")
            )["return_freight_exp_cents"].sum()
        )
        rep.add(
            "return_freight_allocation",
            int(return_freight_log["amount_cents"].sum()),
            actual_alloc + residuals.get("return_freight_unallocated_cents", 0),
        )
    # 6. Stage identity on sale lines: pocket = invoice - deductions + credits.
    sale = wf.filter(pl.col("transaction_type") == "sale")
    lhs = int(sale["pocket_revenue_cents"].sum())
    rhs = int(
        sale.select(
            (
                pl.col("invoice_revenue_cents")
                - pl.col("rebate_actual_cents")
                - pl.col("rebate_settlement_alloc_cents")
                - pl.col("freight_concession_cents")
                + pl.col("price_credit_alloc_cents")
            ).sum()
        ).item()
    )
    rep.add("pocket_stage_identity_sales", rhs, lhs)
    # 7. Contribution identity where cost exists.
    with_cost = sale.filter(pl.col("product_cost_cents").is_not_null())
    lhs2 = int(with_cost["contribution_cents"].sum())
    rhs2 = int(
        with_cost.select(
            (
                pl.col("pocket_revenue_cents")
                - pl.col("product_cost_cents")
                - pl.col("cost_to_serve_cents")
            ).sum()
        ).item()
    )
    rep.add("contribution_identity", rhs2, lhs2)
    return rep
