"""Cost-to-serve activity costing and order economics (Phase 13, reworked 17.1).

Activity rates come from config: per order + per delivery (split exactly across
the order's sale lines, largest-remainder; deliveries proxied by orders when no
delivery table exists — labeled), per line, special handling per unit. Return
handling is covered by the returns policy and is not double counted here.

Measures (all exact cents):
- contribution_cents (BASE): pocket - product cost - waterfall cost-to-serve,
  computed on the COVERED basis (lines with known cost only). Coverage columns
  (revenue_with_cost, pct_revenue_excluded) make the exclusion visible, and
  pocket_covered - product_cost - cts_covered == contribution holds exactly.
- contribution_after_activity_cents: base minus activity cost-to-serve,
  labeled `estimated` (activity rates are config estimates).

Order and customer economics include linked returns (by order_id) with sales
and returns contribution shown separately plus the net; service-policy flags
use the net (after activity — order patterns are exactly what activity costs
price in).
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
    """Add activity_cts_cents and contribution_after_activity_cents per line.
    The base contribution and waterfall cost_to_serve stay untouched."""
    c = rules.cost_to_serve
    notes = [
        "deliveries proxied by orders (no delivery table); per_delivery rate "
        "applied once per order, labeled estimated",
        "activity cost-to-serve is a config estimate; contribution_after_activity "
        "is labeled estimated and the base contribution is kept",
    ]
    sale = pl.col("transaction_type") == "sale"
    if "order_id" not in lines.columns:
        lines = lines.with_columns(
            pl.when(sale).then(c.per_line_cents).otherwise(0).alias("activity_cts_cents"),
        )
    else:
        per_order_total = c.per_order_cents + c.per_delivery_cents
        # Exact split across the order's SALE lines (largest-remainder with
        # equal weights; returns/credits share the order id but are costed by
        # their own policies).
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
        )
    return lines.with_columns(
        (pl.col("contribution_cents") - pl.col("activity_cts_cents")).alias(
            "contribution_after_activity_cents"
        ),
        pl.lit("estimated").alias("activity_cts_status"),
    ), notes


_ECON_AGGS = [
    pl.len().alias("n_lines"),
    (pl.col("transaction_type") == "sale").sum().alias("n_sale_lines"),
    (pl.col("transaction_type") == "return").sum().alias("n_return_lines"),
    # Revenue: full basis, sales and returns split plus net.
    pl.col("net_amount_cents")
    .filter(pl.col("transaction_type") == "sale")
    .sum()
    .fill_null(0)
    .alias("sales_net_cents"),
    pl.col("net_amount_cents")
    .filter(pl.col("transaction_type") == "return")
    .sum()
    .fill_null(0)
    .alias("returns_net_cents"),
    pl.col("net_amount_cents").sum().alias("net_cents"),
    # Covered basis (cost known): the contribution identity holds here.
    pl.col("net_amount_cents")
    .filter(pl.col("product_cost_cents").is_not_null())
    .sum()
    .fill_null(0)
    .alias("revenue_with_cost_cents"),
    pl.col("pocket_revenue_cents")
    .filter(pl.col("product_cost_cents").is_not_null())
    .sum()
    .fill_null(0)
    .alias("pocket_covered_cents"),
    pl.col("product_cost_cents").sum().fill_null(0).alias("product_cost_cents"),
    pl.col("cost_to_serve_cents")
    .filter(pl.col("product_cost_cents").is_not_null())
    .sum()
    .fill_null(0)
    .alias("cts_covered_cents"),
    pl.col("contribution_cents")
    .filter((pl.col("transaction_type") == "sale") & pl.col("product_cost_cents").is_not_null())
    .sum()
    .fill_null(0)
    .alias("sales_contribution_cents"),
    pl.col("contribution_cents")
    .filter((pl.col("transaction_type") == "return") & pl.col("product_cost_cents").is_not_null())
    .sum()
    .fill_null(0)
    .alias("returns_contribution_cents"),
    pl.col("contribution_cents").sum().fill_null(0).alias("contribution_cents"),
    pl.col("activity_cts_cents").sum().fill_null(0).alias("activity_cts_cents"),
]


def _with_coverage(df: pl.DataFrame) -> pl.DataFrame:
    # Group-level after-activity is an explicit formula (base contribution on
    # the covered basis minus the group's FULL activity cost), not a sum of
    # line values — uncovered lines still incur activity.
    df = df.with_columns(
        (pl.col("contribution_cents") - pl.col("activity_cts_cents")).alias(
            "contribution_after_activity_cents"
        )
    )
    return df.with_columns(
        pl.when(pl.col("net_cents").abs() > 0)
        .then(
            1.0
            - pl.col("revenue_with_cost_cents").cast(pl.Float64)
            / pl.col("net_cents").cast(pl.Float64)
        )
        .otherwise(0.0)
        .alias("pct_revenue_excluded"),
        pl.lit("estimated").alias("activity_cts_status"),
    )


def order_economics(lines: pl.DataFrame) -> pl.DataFrame:
    base = lines.filter(pl.col("transaction_type").is_in(["sale", "return"]))
    out = (
        base.group_by("order_id")
        .agg(
            pl.col("customer_id").first(),
            pl.col("invoice_date")
            .filter(pl.col("transaction_type") == "sale")
            .first()
            .alias("order_date"),
            *_ECON_AGGS,
        )
        .sort("order_id")
    )
    return _with_coverage(out)


def customer_economics(lines: pl.DataFrame) -> pl.DataFrame:
    months = (
        lines.filter(pl.col("transaction_type") == "sale")
        .select(pl.col("invoice_date").dt.strftime("%Y-%m").n_unique())
        .item()
    )
    months = max(int(months or 1), 1)
    base = lines.filter(pl.col("transaction_type").is_in(["sale", "return"]))
    out = (
        base.group_by("customer_id")
        .agg(
            pl.col("order_id")
            .filter(pl.col("transaction_type") == "sale")
            .n_unique()
            .alias("n_orders"),
            *_ECON_AGGS,
        )
        .with_columns(
            (pl.col("sales_net_cents") / pl.col("n_orders").clip(lower_bound=1)).alias(
                "avg_order_value_cents"
            ),
        )
        .with_columns((pl.col("n_orders") / months).alias("orders_per_month"))
    )
    return _with_coverage(out).sort(["contribution_after_activity_cents", "customer_id"])


def service_policy_flags(customers_econ: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    """Service-policy opportunities, strictly separate from pricing: customers
    whose order pattern destroys NET contribution after activity costs."""
    c = rules.cost_to_serve
    return (
        customers_econ.with_columns(
            (
                (pl.col("avg_order_value_cents") < c.min_order_value_cents)
                & (pl.col("contribution_after_activity_cents") < 0)
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
    if "order_id" not in lines.columns:
        # Real exports often carry no order number (Online Retail II). The
        # invoice is the natural order proxy - one document, one shipment -
        # and the proxy is stated, never silent.
        lines = lines.with_columns(pl.col("invoice_id").alias("order_id"))
        lines_proxy_note = [
            "no order_id in the mapping: the invoice is used as the order unit "
            "for order economics and activity costs (labeled proxy)"
        ]
    else:
        lines_proxy_note = []
    lines, notes = apply_activity_costs(lines, rules)
    notes = lines_proxy_note + notes
    orders = order_economics(lines)
    cust = customer_economics(lines)
    flags = service_policy_flags(cust, rules)
    return CostToServeResult(
        lines=lines,
        order_economics=orders,
        customer_economics=cust,
        service_flags=flags,
        notes=notes,
    )
