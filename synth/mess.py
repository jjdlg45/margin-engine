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
    outliers (deep one-off discounts, 50-60% off the realized price, e.g.
    clearance or a strategic concession). Net and discount stay consistent."""
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
    factor[comm] = rng.uniform(0.40, 0.50, int(comm.sum()))

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


RETURN_REASON_CODES = ["CUST_ERR", "DIST_ERR", "DEFECT"]
RETURN_REASON_P = [0.5, 0.2, 0.3]
RESTOCK_FEE_BP = 1500  # planted policy: 15% restocking fee on customer-error returns
RETURN_FREIGHT_FIXED_CENTS = 1000
RETURN_FREIGHT_CENTS_PER_LB = 10


def add_returns(
    cfg: GeneratorConfig, lines: pl.DataFrame, months: int, rng: np.random.Generator
) -> pl.DataFrame:
    """Linked partial returns 1-2 months after the sale, within horizon.

    Return economics planted per reason: customer_error returns carry a 15%
    restocking fee and the customer pays return freight (billed = expense);
    distributor_error and defective returns have no recovery and the
    distributor eats the return freight.
    """
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

    reason = rng.choice(RETURN_REASON_CODES, k, p=RETURN_REASON_P)
    restock_fee = np.where(reason == "CUST_ERR", apply_bp(-net, RESTOCK_FEE_BP), 0)
    weight = cand["weight_lb"].to_numpy()
    rtn_frt_exp = RETURN_FREIGHT_FIXED_CENTS + np.round(
        -ret_qty * weight * RETURN_FREIGHT_CENTS_PER_LB
    ).astype(np.int64)
    rtn_frt_billed = np.where(reason == "CUST_ERR", rtn_frt_exp, 0)

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
        pl.Series("return_reason_code", reason),
        pl.Series("restock_fee_cents", restock_fee.astype(np.int64)),
        pl.Series("rtn_frt_billed_cents", rtn_frt_billed.astype(np.int64)),
        pl.Series("truth_rtn_frt_exp_cents", rtn_frt_exp),
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


def add_kits(
    cfg: GeneratorConfig, lines: pl.DataFrame, items: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """Kits (Phase 12): ~1.5% of sale lines become kit parents; 2-3 component
    lines ride along at zero price on the same invoice, sharing a KIT id. The
    parent's net is the kit's revenue; components carry quantity and cost."""
    sales_mask = (lines["txn_type"] == "sale").to_numpy() & (lines["quantity"].to_numpy() > 0)
    pick = (rng.random(lines.height) < 0.015) & sales_mask
    parents = lines.filter(pl.Series(pick))
    if parents.height == 0:
        return lines.with_columns(pl.lit(None, dtype=pl.String).alias("kit_id"))
    k = parents.height
    kit_ids = [f"KIT{50000 + i}" for i in range(k)]
    parents = parents.with_columns(pl.Series("kit_id", kit_ids))
    lines = lines.with_columns(pl.lit(None, dtype=pl.String).alias("kit_id"))
    # Mark the sampled originals as parents.
    lines = (
        lines.join(
            parents.select("line_seq", pl.col("kit_id").alias("_kid")),
            on="line_seq",
            how="left",
        )
        .with_columns(pl.coalesce(pl.col("_kid"), pl.col("kit_id")).alias("kit_id"))
        .drop("_kid")
    )
    item_pool = items.filter(~pl.col("is_service"))
    n_comp = rng.integers(2, 4, k)
    rep_idx = np.repeat(np.arange(k), n_comp)
    total = len(rep_idx)
    comp_base = parents[rep_idx.tolist()]
    comp_items = item_pool[rng.integers(0, item_pool.height, total).tolist()]
    qty = rng.integers(1, 4, total)
    list_val = qty * comp_items["base_list_cents"].to_numpy()
    # Components are real purchases: vendor rebates accrue on their cost at the
    # component supplier's planted rate (looked up from existing lines).
    vr_by_supplier = dict(
        lines.filter(pl.col("vendor_rebate_bp").is_not_null())
        .group_by("supplier_id")
        .agg(pl.col("vendor_rebate_bp").max())
        .iter_rows()
    )
    comp_vr_bp = np.array(
        [int(vr_by_supplier.get(s_, 0) or 0) for s_ in comp_items["supplier_id"].to_list()],
        dtype=np.int64,
    )
    comp_cost_ext = qty.astype(np.int64) * comp_items["base_cost_cents"].to_numpy()
    comp_vendor_truth = apply_bp(comp_cost_ext, comp_vr_bp)
    comps = comp_base.with_columns(
        pl.Series("item_id", comp_items["item_id"]),
        pl.Series("quantity", qty.astype(np.int64)),
        pl.Series("uom", comp_items["base_uom"]),
        pl.lit(0, dtype=pl.Int64).alias("unit_price_cents"),
        pl.Series("unit_cost_cents", comp_items["base_cost_cents"]),
        pl.Series("unit_list_cents", comp_items["base_list_cents"]),
        pl.lit(0, dtype=pl.Int64).alias("net_cents"),
        pl.Series("list_value_cents", list_val.astype(np.int64)),
        pl.Series("discount_cents", list_val.astype(np.int64)),
        pl.lit(0, dtype=pl.Int64).alias("rebate_line_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_settlement_rebate_cents"),
        pl.lit(0, dtype=pl.Int64).alias("freight_concession_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_freight_expense_cents"),
        pl.Series("truth_vendor_rebate_cents", comp_vendor_truth),
        pl.Series("vendor_rebate_bp", comp_vr_bp),
        pl.lit(0, dtype=pl.Int64).alias("truth_order_billed_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_tiered_accrual_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_tiered_rate_bp"),
        pl.Series("supplier_id", comp_items["supplier_id"]),
        pl.Series("item_category", comp_items["item_category"]),
        pl.Series("weight_lb", comp_items["weight_lb"]),
        pl.lit(False).alias("is_service"),
        pl.lit(False).alias("cost_missing"),
        pl.Series("truth_unit_cost_cents", comp_items["base_cost_cents"]),
    )
    out = pl.concat([lines, comps], how="diagonal_relaxed")
    # Re-sequence so every row keeps a stable, unique line_seq.
    return out.drop("line_seq").with_row_index("line_seq")


def add_free_goods(
    cfg: GeneratorConfig, lines: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """Free goods (Phase 12): ~0.4% of sale lines spawn a zero-price promo line
    of the same item on the same invoice, transaction type F."""
    sales_mask = (lines["txn_type"] == "sale").to_numpy() & (lines["quantity"].to_numpy() > 0)
    pick = (rng.random(lines.height) < 0.004) & sales_mask
    base = lines.filter(pl.Series(pick))
    if base.height == 0:
        return lines
    qty = np.maximum(1, (base["quantity"].to_numpy() * 0.1).astype(np.int64))
    free = base.with_columns(
        pl.Series("quantity", qty),
        pl.lit(False).alias("cost_missing"),
        pl.coalesce(pl.col("truth_unit_cost_cents"), pl.col("unit_cost_cents")).alias(
            "unit_cost_cents"
        ),
        pl.lit(0, dtype=pl.Int64).alias("unit_price_cents"),
        pl.lit(0, dtype=pl.Int64).alias("net_cents"),
        (pl.Series("_q", qty) * pl.col("unit_list_cents")).alias("list_value_cents"),
        (pl.Series("_q2", qty) * pl.col("unit_list_cents")).alias("discount_cents"),
        pl.lit("free_good").alias("txn_type"),
        pl.lit(0, dtype=pl.Int64).alias("rebate_line_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_settlement_rebate_cents"),
        pl.lit(0, dtype=pl.Int64).alias("freight_concession_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_freight_expense_cents"),
        pl.lit(0, dtype=pl.Int64).alias("truth_vendor_rebate_cents"),
        pl.lit(None, dtype=pl.String).alias("kit_id"),
    )
    out = pl.concat([lines, free], how="diagonal_relaxed")
    return out.drop("line_seq").with_row_index("line_seq")
