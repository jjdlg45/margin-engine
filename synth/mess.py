"""Mess injection: everything a real ERP export does to ruin your day.

Each transformation runs at its configured rate and records planted truth so
recovery is testable. Price corruption runs BEFORE deduction truths are
computed, so rebates and freight stay internally consistent with the corrupted
prices (as they would in a real system where the error happened at entry).
"""

from __future__ import annotations

import numpy as np
import polars as pl

from synth.config import GeneratorConfig
from synth.money import apply_bp

ZERO_QTY_FEE_CENTS = 2500
CREDIT_REASONS = ["price_correction", "damage", "shortage", "quality"]


def corrupt_prices(
    cfg: GeneratorConfig, lines: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """Plant data-entry price errors (x10 or /10) and genuine commercial
    outliers (extra ~25% discount). Net and discount stay consistent."""
    n = lines.height
    r = rng.random(n)
    data_err = r < cfg.mess.price_outlier_rate
    comm = (r >= cfg.mess.price_outlier_rate) & (
        r < cfg.mess.price_outlier_rate + cfg.mess.commercial_outlier_rate
    )
    factor = np.ones(n)
    err_dir = rng.random(n) < 0.5
    factor[data_err & err_dir] = 10.0
    factor[data_err & ~err_dir] = 0.1
    factor[comm] = 0.75

    price = lines["unit_price_cents"].to_numpy()
    new_price = np.maximum(1, np.round(price * factor).astype(np.int64))
    qty = lines["quantity"].to_numpy()
    net = qty * new_price
    outlier_type = np.where(data_err, "data_quality", np.where(comm, "commercial", ""))
    return lines.with_columns(
        pl.Series("unit_price_cents", new_price),
        pl.Series("net_cents", net),
        (pl.col("list_value_cents") - pl.Series("_n", net)).alias("discount_cents"),
        pl.Series("truth_outlier_type", outlier_type),
    )


def add_returns(
    cfg: GeneratorConfig, lines: pl.DataFrame, months: int, rng: np.random.Generator
) -> pl.DataFrame:
    """Linked partial returns 1-2 months after the sale, within horizon."""
    n = lines.height
    pick = rng.random(n) < cfg.mess.return_rate
    cand = lines.filter(pl.Series(pick) & (pl.col("month") <= months - 2))
    if cand.height == 0:
        return lines
    k = cand.height
    frac = rng.uniform(0.2, 1.0, k)
    qty = cand["quantity"].to_numpy()
    ret_qty = -np.maximum(1, np.ceil(qty * frac)).astype(np.int64)
    lag = rng.integers(1, 3, k)
    ret_month = np.minimum(cand["month"].to_numpy() + lag, months - 1)
    price = cand["unit_price_cents"].to_numpy()
    net = ret_qty * price
    rate = cand["rate_bp"].fill_null(0).to_numpy()
    kind = cand["program_kind"].fill_null("none").to_numpy()
    reb = apply_bp(net, rate)
    returns = cand.with_columns(
        pl.Series("quantity", ret_qty),
        pl.Series("month", ret_month),
        pl.Series("net_cents", net),
        (pl.col("unit_list_cents") * pl.Series("_q", ret_qty)).alias("list_value_cents"),
        pl.lit("return").alias("txn_type"),
        pl.col("invoice_id").alias("original_invoice_ref"),
        pl.Series("rebate_line_cents", np.where(kind == "line_accrual", reb, 0)),
        pl.Series("truth_settlement_rebate_cents", np.where(kind == "settlement", reb, 0)),
        pl.lit(0, dtype=pl.Int64).alias("freight_concession_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_freight_expense_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_vendor_rebate_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_order_billed_cents"),
        pl.lit("").alias("truth_outlier_type"),
    ).with_columns(
        (pl.col("list_value_cents") - pl.col("net_cents")).alias("discount_cents"),
        (pl.lit("RET") + pl.col("invoice_id").str.slice(3)).alias("invoice_id"),
        (pl.col("invoice_date").dt.offset_by(pl.format("{}mo", pl.Series("_lag", lag)))).alias(
            "invoice_date"
        ),
    )
    return pl.concat([lines, returns], how="diagonal_relaxed")


def add_credit_memos(
    cfg: GeneratorConfig, lines: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """One credit line per sampled invoice, classified by reason code."""
    invoices = (
        lines.filter(pl.col("txn_type") == "sale")
        .group_by("invoice_id")
        .agg(
            pl.col("net_cents").sum().alias("inv_net"),
            pl.col("customer_id").first(),
            pl.col("item_id").first(),
            pl.col("item_pos").first(),
            pl.col("month").first(),
            pl.col("segment_id").first(),
            pl.col("invoice_date").first(),
            pl.col("sales_rep_id").first(),
            pl.col("channel").first(),
            pl.col("uom").first(),
            pl.col("supplier_id").first(),
            pl.col("item_category").first(),
            pl.col("weight_lb").first(),
            pl.col("is_service").first(),
            pl.col("_inv_seq").first(),
        )
        .sort("invoice_id")
    )
    pick = rng.random(invoices.height) < cfg.mess.credit_memo_rate
    cand = invoices.filter(pl.Series(pick) & (pl.col("inv_net") > 0))
    if cand.height == 0:
        return lines
    k = cand.height
    reasons = rng.choice(CREDIT_REASONS, k)
    amt_bp = rng.integers(100, 501, k)
    amount = -apply_bp(cand["inv_net"].to_numpy(), amt_bp)
    credits = cand.with_columns(
        pl.lit("credit").alias("txn_type"),
        pl.Series("credit_reason", reasons),
        pl.col("invoice_id").alias("original_invoice_ref"),
        (pl.lit("CM") + pl.col("invoice_id").str.slice(3)).alias("invoice_id"),
        pl.Series("net_cents", amount),
        pl.lit(0, dtype=pl.Int64).alias("quantity"),
        pl.lit(0, dtype=pl.Int64).alias("unit_price_cents"),
        pl.lit(0, dtype=pl.Int64).alias("unit_cost_cents"),
        pl.lit(0, dtype=pl.Int64).alias("unit_list_cents"),
        pl.lit(0, dtype=pl.Int64).alias("list_value_cents"),
        pl.lit(0, dtype=pl.Int64).alias("discount_cents"),
        pl.lit(0, dtype=pl.Int64).alias("rebate_line_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_settlement_rebate_cents"),
        pl.lit(0, dtype=pl.Int64).alias("freight_concession_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_freight_expense_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_vendor_rebate_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_order_billed_cents"),
        pl.lit("").alias("truth_outlier_type"),
    ).drop("inv_net")
    return pl.concat([lines, credits], how="diagonal_relaxed")


def add_zero_qty_rows(
    cfg: GeneratorConfig, lines: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """Malformed zero-quantity rows with a nonzero amount (fee or bad entry)."""
    sales = lines.filter(pl.col("txn_type") == "sale")
    k = round(sales.height * cfg.mess.zero_qty_rate)
    if k == 0:
        return lines.with_columns(pl.lit(False).alias("truth_zero_qty"))
    idx = rng.choice(sales.height, k, replace=False)
    rows = sales[sorted(idx.tolist())].with_columns(
        pl.lit(0, dtype=pl.Int64).alias("quantity"),
        pl.lit(0, dtype=pl.Int64).alias("unit_price_cents"),
        pl.lit(ZERO_QTY_FEE_CENTS, dtype=pl.Int64).alias("net_cents"),
        pl.lit(0, dtype=pl.Int64).alias("list_value_cents"),
        pl.lit(-ZERO_QTY_FEE_CENTS, dtype=pl.Int64).alias("discount_cents"),
        pl.lit(0, dtype=pl.Int64).alias("rebate_line_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_settlement_rebate_cents"),
        pl.lit(0, dtype=pl.Int64).alias("freight_concession_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_freight_expense_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_vendor_rebate_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_order_billed_cents"),
        pl.lit(True).alias("truth_zero_qty"),
    )
    lines = lines.with_columns(pl.lit(False).alias("truth_zero_qty"))
    return pl.concat([lines, rows], how="diagonal_relaxed")


def blank_costs(
    cfg: GeneratorConfig, lines: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """Blank unit cost on a sample of non-service sale lines; truth keeps it."""
    n = lines.height
    eligible = (
        (lines["txn_type"] == "sale") & (~lines["is_service"]) & (lines["quantity"] > 0)
    ).to_numpy()
    pick = (rng.random(n) < cfg.mess.missing_cost_rate) & eligible
    cost = lines["unit_cost_cents"].to_numpy()
    return lines.with_columns(
        pl.Series("truth_unit_cost_cents", cost),
        pl.Series("cost_missing", pick),
        pl.Series("unit_cost_cents", np.where(pick, 0, cost)),
    )


def reexpress_uom(
    cfg: GeneratorConfig,
    lines: pl.DataFrame,
    uom_conversions: pl.DataFrame,
    rng: np.random.Generator,
) -> pl.DataFrame:
    """Express a sample of lines in the alternate unit (EA instead of CS/BX).
    Net amount is untouched, so reconciliation stays exact; quantity and UOM
    change and must be normalized back through the conversion table."""
    if uom_conversions.height == 0:
        return lines.with_columns(pl.lit(False).alias("truth_uom_alt"))
    conv = uom_conversions.rename({"to_uom": "uom"}).select("item_id", "uom", "factor")
    lines = lines.join(conv, on=["item_id", "uom"], how="left")
    n = lines.height
    has_conv = lines["factor"].is_not_null().to_numpy()
    is_sale = (lines["txn_type"] == "sale").to_numpy() & (lines["quantity"].to_numpy() > 0)
    pick = (rng.random(n) < cfg.mess.uom_alt_rate) & has_conv & is_sale
    factor = lines["factor"].fill_null(1).to_numpy()
    qty = lines["quantity"].to_numpy()
    uom = lines["uom"].to_numpy()
    return lines.drop("factor").with_columns(
        pl.Series("quantity", np.where(pick, qty * factor, qty)),
        pl.Series("uom", np.where(pick, "EA", uom)),
        pl.Series("truth_uom_alt", pick),
    )


def add_duplicates(
    cfg: GeneratorConfig, lines: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """Verified duplicates re-export the same source line key; look-alikes get a
    fresh key and must be flagged, never removed. Requires line_key assigned."""
    n = lines.height
    r = rng.random(n)
    ver = r < cfg.mess.verified_dup_rate
    look = (r >= cfg.mess.verified_dup_rate) & (
        r < cfg.mess.verified_dup_rate + cfg.mess.lookalike_dup_rate
    )
    lines = lines.with_columns(
        pl.Series(
            "truth_dup_type", np.where(ver, "verified_source", np.where(look, "lookalike", ""))
        )
    )
    ver_rows = lines.filter(pl.Series(ver))  # same line_key: true re-export
    look_rows = lines.filter(pl.Series(look)).with_columns(
        (pl.col("line_key") + pl.lit("X")).alias("line_key"),
        pl.lit("lookalike_copy").alias("truth_dup_type"),
    )
    return pl.concat([lines, ver_rows, look_rows], how="vertical")
