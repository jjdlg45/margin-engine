"""Turn bought pair-months into invoice lines with exact money components.

Output is a canonical-schema Polars frame in integer cents. Mess injection and
ERP renaming happen later. Per-line planted truths (rebates, freight expense,
vendor rebates, landed components) are carried as truth_* columns and split out
into ground-truth files at export time.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from synth.config import GeneratorConfig
from synth.demand import DemandWorld
from synth.entities import month_starts
from synth.money import apply_bp, div_round_half_even

LINES_PER_INVOICE = 8
FREIGHT_RATE_CENTS_PER_LB_MIN = 8
FREIGHT_RATE_CENTS_PER_LB_MAX = 20
FREIGHT_FIXED_CENTS = 1500  # fixed component per order, makes allocation non-trivial
INBOUND_FREIGHT_CENTS_PER_LB = 5
BASE_DUTY_BP = {0: 300, 1: 500, 2: 0, 3: 800, 4: 200, 5: 600, 6: 0, 7: 400}


def build_lines(
    cfg: GeneratorConfig,
    world: DemandWorld,
    customers: pl.DataFrame,
    items: pl.DataFrame,
    rng: np.random.Generator,
) -> pl.DataFrame:
    starts = month_starts(cfg)
    sold = world.panel.filter(pl.col("bought") & (pl.col("qty") > 0)).sort(
        "customer_id", "month", "item_id"
    )

    # Planted unprofitable small-order customers (Phase 13): ~6% of customers
    # place every line as its own tiny order. Separate RNG stream keeps other
    # worlds' draws unchanged.
    so_rng = np.random.default_rng(cfg.seed + 444)
    cust_ids = sorted(sold["customer_id"].unique().to_list())
    small_order_customers = sorted(c for c in cust_ids if so_rng.random() < 0.06)
    is_small = pl.col("customer_id").is_in(small_order_customers)
    sold = sold.with_columns(
        pl.when(is_small)
        .then((pl.col("qty") * 0.15).ceil().clip(lower_bound=1).cast(pl.Int64))
        .otherwise(pl.col("qty"))
        .alias("qty")
    )

    # --- invoice grouping: chunks of <= LINES_PER_INVOICE per customer-month ---
    sold = sold.with_columns(
        (pl.col("item_id").cum_count().over("customer_id", "month") - 1).alias("_ln")
    ).with_columns(
        pl.when(is_small)
        .then(pl.col("_ln"))  # one line per order
        .otherwise(pl.col("_ln") // LINES_PER_INVOICE)
        .alias("_chunk")
    )
    inv_keys = (
        sold.select("customer_id", "month", "_chunk")
        .unique(maintain_order=True)
        .with_row_index("_inv_seq")
    )
    sold = sold.join(inv_keys, on=["customer_id", "month", "_chunk"], how="left")

    n = sold.height
    day_offset = rng.integers(0, 28, n)
    month_idx = sold["month"].to_numpy()
    inv_dates = [
        starts[m].replace(day=1 + int(d)) for m, d in zip(month_idx, day_offset, strict=False)
    ]
    # All lines on one invoice share the invoice date: take the first line's date.
    sold = sold.with_columns(pl.Series("_raw_date", inv_dates, dtype=pl.Date)).with_columns(
        pl.col("_raw_date").first().over("_inv_seq").alias("invoice_date"),
        (pl.lit("INV") + (pl.col("_inv_seq") + 100001).cast(pl.String)).alias("invoice_id"),
        (pl.lit("ORD") + (pl.col("_inv_seq") + 500001).cast(pl.String)).alias("order_id"),
    )

    qty = sold["qty"].to_numpy()
    price = sold["unit_price_cents"].to_numpy()
    cost = sold["unit_cost_cents"].to_numpy()
    list_c = sold["list_cents"].to_numpy()
    ext_net = qty * price
    ext_list = qty * list_c
    discount = ext_list - ext_net  # on-invoice discount dollars (can be negative)

    base = sold.select(
        "customer_id",
        "item_id",
        "item_pos",
        "month",
        "segment_id",
        "on_contract",
        "contract_id",
        "invoice_date",
        "invoice_id",
        "order_id",
        "_inv_seq",
    ).with_columns(
        pl.Series("quantity", qty),
        pl.Series("unit_price_cents", price),
        pl.Series("unit_cost_cents", cost),
        pl.Series("unit_list_cents", list_c),
        pl.Series("net_cents", ext_net),
        pl.Series("list_value_cents", ext_list),
        pl.Series("discount_cents", discount),
        pl.lit("sale").alias("txn_type"),
    )

    # --- customer attributes (rep, channel) ------------------------------
    base = base.join(
        customers.select("customer_id", "sales_rep_id", "channel"), on="customer_id", how="left"
    ).with_columns(
        pl.col("customer_id").is_in(small_order_customers).alias("truth_small_order_customer")
    )
    # --- item attributes --------------------------------------------------
    base = base.join(
        items.select(
            "item_id", "base_uom", "weight_lb", "supplier_id", "item_category", "is_service"
        ),
        on="item_id",
        how="left",
    ).rename({"base_uom": "uom"})
    return base


def add_deductions(
    cfg: GeneratorConfig,
    lines: pl.DataFrame,
    rebate_programs: pl.DataFrame,
    world: DemandWorld,
    rng: np.random.Generator,
) -> pl.DataFrame:
    """Rebates (line-accrued and settlement truth), freight (billed, concession,
    expense), vendor rebates, landed components. All exact integer cents."""
    n = lines.height
    lines = lines.join(
        rebate_programs.select("customer_id", "program_kind", "rate_bp", "rebate_program_id"),
        on="customer_id",
        how="left",
    )
    net = lines["net_cents"].to_numpy()
    kind = lines["program_kind"].fill_null("none").to_numpy()
    rate = lines["rate_bp"].fill_null(0).to_numpy()
    rebate_truth = apply_bp(net, rate)
    line_rebate = np.where(kind == "line_accrual", rebate_truth, 0)
    settle_rebate_truth = np.where(kind == "settlement", rebate_truth, 0)

    # --- freight: order level draws --------------------------------------
    inv_seq = lines["_inv_seq"].to_numpy()
    qty = lines["quantity"].to_numpy()
    weight = lines["weight_lb"].to_numpy()
    line_wt_centilb = np.round(qty * weight * 100).astype(np.int64)

    n_orders = int(inv_seq.max()) + 1 if n else 0
    order_rate = rng.integers(
        FREIGHT_RATE_CENTS_PER_LB_MIN, FREIGHT_RATE_CENTS_PER_LB_MAX + 1, n_orders
    )
    order_billed_mask = rng.random(n_orders) < cfg.freight_billed_share
    order_conc_mask = rng.random(n_orders) < cfg.freight_concession_share
    conc_bp_order = rng.integers(100, 301, n_orders)  # 1-3% of line net

    rate_l = order_rate[inv_seq]
    # Variable expense per line: weight(centi-lb) * rate(cents/lb) / 100, half-even.
    var_exp = div_round_half_even(line_wt_centilb * rate_l, 100)
    # Fixed per-order component split equally across lines, residual to largest line.
    order_sizes = np.bincount(inv_seq, minlength=n_orders)
    fixed_share = div_round_half_even(
        np.full(n, FREIGHT_FIXED_CENTS, dtype=np.int64), order_sizes[inv_seq]
    )
    # Correct the per-order rounding drift deterministically on the max-weight line.
    fixed_sum = np.bincount(inv_seq, weights=fixed_share, minlength=n_orders).astype(np.int64)
    residual = FREIGHT_FIXED_CENTS - fixed_sum
    # index of heaviest line per order
    order_max = np.zeros(n_orders, dtype=np.int64)
    np.maximum.at(order_max, inv_seq, line_wt_centilb)
    is_max = line_wt_centilb == order_max[inv_seq]
    # First max-weight line per order takes the residual (deterministic).
    max_idx = np.flatnonzero(is_max)
    _, first_pos = np.unique(inv_seq[max_idx], return_index=True)
    first_max = np.zeros(n, dtype=bool)
    first_max[max_idx[first_pos]] = True
    fixed_share = fixed_share + np.where(first_max, residual[inv_seq], 0)
    freight_exp_truth = var_exp + fixed_share

    conc = np.where(order_conc_mask[inv_seq], apply_bp(net, conc_bp_order[inv_seq]), 0)
    order_billed_total = np.bincount(
        inv_seq, weights=np.where(order_billed_mask[inv_seq], var_exp, 0), minlength=n_orders
    ).astype(np.int64)
    # Billed freight marked up 10% over variable expense, order level.
    order_billed_total = np.where(
        order_billed_mask, div_round_half_even(order_billed_total * 110, 100), 0
    )

    # --- vendor rebates ---------------------------------------------------
    suppliers = sorted(lines["supplier_id"].unique().to_list())
    vr_rng = rng.random(len(suppliers))
    vr_map = {
        s: (
            int(cfg.vendor_rebate_rate * 10_000)
            if vr_rng[i] < cfg.vendor_rebate_supplier_share
            else 0
        )
        for i, s in enumerate(suppliers)
    }
    vr_bp = np.array([vr_map[s] for s in lines["supplier_id"].to_list()])
    cost_ext = lines["unit_cost_cents"].to_numpy() * qty
    vendor_rebate_truth = apply_bp(cost_ext, vr_bp)

    # --- landed components ------------------------------------------------
    inb_pu = np.round(weight * 100 * INBOUND_FREIGHT_CENTS_PER_LB / 100).astype(np.int64)
    cat_num = np.array([int(c[3:]) for c in lines["item_category"].to_list()])
    month = lines["month"].to_numpy()
    base_duty = np.array([BASE_DUTY_BP.get(c % 8, 0) for c in cat_num])
    zt = world.z_tariff[cat_num % world.z_tariff.shape[0], month]
    duty_bp = np.round(base_duty * np.exp(zt)).astype(np.int64)
    duty_pu = div_round_half_even(lines["unit_cost_cents"].to_numpy() * duty_bp, 10_000)

    return lines.with_columns(
        pl.Series("rebate_line_cents", line_rebate),
        pl.Series("truth_settlement_rebate_cents", settle_rebate_truth),
        pl.Series("freight_concession_cents", conc),
        pl.Series("truth_freight_expense_cents", freight_exp_truth),
        pl.Series("truth_vendor_rebate_cents", vendor_rebate_truth),
        pl.Series("truth_order_billed_cents", order_billed_total[inv_seq]),
        pl.Series("inbound_freight_pu_cents", inb_pu),
        pl.Series("duty_pu_cents", duty_pu),
        pl.Series("vendor_rebate_bp", vr_bp),
    )
