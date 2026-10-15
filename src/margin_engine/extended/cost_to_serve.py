"""Cost-to-serve activity costing and order economics (Phase 13).

Activity rates come from config: per order (allocated across the order's sale
lines, largest-remainder with equal weights), per line (direct), per delivery
(proxied by orders when no delivery table exists — labeled), special handling
per unit. Return handling is covered by the returns policy (Phase 8.2) and is
not double counted here.

Outputs: per-line activity cost (added to cost_to_serve), order-level and
customer-level contribution, and SERVICE-POLICY flags (minimum order value,
delivery frequency) kept strictly separate from pricing opportunities.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from margin_engine.config import Rules


@dataclass
class CostToServeResult:
    lines: pl.DataFrame
    order_economics: pl.DataFrame
    customer_economics: pl.DataFrame
    service_flags: pl.DataFrame
    notes: list[str]


def apply_activity_costs(lines: pl.DataFrame, rules: Rules) -> tuple[pl.DataFrame, list[str]]:
    """Add activity_cts_cents per sale line and fold it into cost_to_serve."""
    c = rules.cost_to_serve
    notes = [
        "deliveries proxied by orders (no delivery table); per_delivery rate "
        "applied once per order, labeled estimated"
    ]
    sale = pl.col("transaction_type") == "sale"
    if "order_id" not in lines.columns:
        lines = lines.with_columns(
            pl.when(sale).then(c.per_line_cents).otherwise(0).alias("activity_cts_cents"),
            pl.lit("estimated").alias("activity_cts_status"),
        )
    else:
        per_order_total = c.per_order_cents + c.per_delivery_cents
        # Per-order cost is carried by the order's SALE lines (returns and
        # credits share the original order id but are costed by their own
        # policies). Exact split: floor share + leftover cents to the first
        # sale lines by source_line_key (largest-remainder with equal weights).
        n_sale_in_order = (
            sale.cast(pl.Int64).sum().over("order_id").clip(lower_bound=1).cast(pl.Int64)
        )
        sale_rank = (
            pl.when(sale)
            .then(pl.col("source_line_key"))
            .otherwise(None)
            .rank("ordinal")
            .over("order_id")
            - 1
        )
        order_share = pl.lit(per_order_total, dtype=pl.Int64).floordiv(n_sale_in_order) + (
            sale_rank < (pl.lit(per_order_total, dtype=pl.Int64) % n_sale_in_order)
        ).cast(pl.Int64)
        qty_units = pl.col("quantity_norm").abs().round(0).cast(pl.Int64)
        lines = lines.with_columns(
            pl.when(sale)
            .then(order_share + c.per_line_cents + c.special_handling_per_unit_cents * qty_units)
            .otherwise(0)
            .alias("activity_cts_cents"),
            pl.lit("estimated").alias("activity_cts_status"),
        )
    lines = lines.with_columns(
        (pl.col("cost_to_serve_cents") + pl.col("activity_cts_cents")).alias("cost_to_serve_cents"),
        (pl.col("contribution_cents") - pl.col("activity_cts_cents")).alias("contribution_cents"),
    )
    return lines, notes


def order_economics(lines: pl.DataFrame) -> pl.DataFrame:
    sale = lines.filter(pl.col("transaction_type") == "sale")
    return (
        sale.group_by("order_id")
        .agg(
            pl.col("customer_id").first(),
            pl.col("invoice_date").first().alias("order_date"),
            pl.len().alias("n_lines"),
            pl.col("net_amount_cents").sum().alias("net_cents"),
            pl.col("pocket_revenue_cents").sum().alias("pocket_cents"),
            pl.col("product_cost_cents").sum().alias("product_cost_cents"),
            pl.col("cost_to_serve_cents").sum().alias("cost_to_serve_cents"),
            pl.col("contribution_cents").sum().alias("contribution_cents"),
        )
        .sort("order_id")
    )


def customer_economics(orders: pl.DataFrame, lines: pl.DataFrame) -> pl.DataFrame:
    months = (
        lines.filter(pl.col("transaction_type") == "sale")
        .select(pl.col("invoice_date").dt.strftime("%Y-%m").n_unique())
        .item()
    )
    months = max(int(months), 1)
    return (
        orders.group_by("customer_id")
        .agg(
            pl.len().alias("n_orders"),
            pl.col("net_cents").sum(),
            pl.col("pocket_cents").sum(),
            pl.col("cost_to_serve_cents").sum(),
            pl.col("contribution_cents").sum(),
            pl.col("net_cents").mean().alias("avg_order_value_cents"),
        )
        .with_columns((pl.col("n_orders") / months).alias("orders_per_month"))
        .sort("contribution_cents")
    )


def service_policy_flags(customers_econ: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Service-policy opportunities, strictly separate from pricing: customers
    whose order pattern (not their prices) destroys contribution."""
    c = rules.cost_to_serve
    return (
        customers_econ.with_columns(
            (
                (pl.col("avg_order_value_cents") < c.min_order_value_cents)
                & (pl.col("contribution_cents") < 0)
            ).alias("flag_minimum_order_policy"),
            (
                (pl.col("orders_per_month") > c.max_orders_per_month)
                & (pl.col("avg_order_value_cents") < c.min_order_value_cents)
            ).alias("flag_delivery_frequency_policy"),
        )
        .filter(pl.col("flag_minimum_order_policy") | pl.col("flag_delivery_frequency_policy"))
        .with_columns(
            pl.lit("service_policy_not_pricing").alias("opportunity_category"),
        )
    )


def run_cost_to_serve(lines: pl.DataFrame, rules: Rules) -> CostToServeResult:
    lines, notes = apply_activity_costs(lines, rules)
    orders = order_economics(lines)
    cust = customer_economics(orders, lines)
    flags = service_policy_flags(cust, rules)
    return CostToServeResult(
        lines=lines,
        order_economics=orders,
        customer_economics=cust,
        service_flags=flags,
        notes=notes,
    )
