"""Export: ERP-styled CSVs (column names deliberately non-canonical),
reference tables, and ground truth files.

Layout:
    <out>/erp/invoice_lines.csv        main export, ERP column names
    <out>/erp/customers.csv, items.csv, price_list.csv, uom_conversions.csv,
         contracts.csv, rebate_programs.csv, rebate_settlements.csv,
         order_charges.csv, freight_log.csv, vendor_rebate_programs.csv,
         supplier_cost_index.csv, tariff_schedule.csv, landed_cost_components.csv
    <out>/ground_truth/truth_summary.json
    <out>/ground_truth/line_truth.parquet
    <out>/ground_truth/panel_truth.parquet
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from synth.config import GeneratorConfig
from synth.demand import DemandWorld
from synth.entities import SegmentDef, month_starts
from synth.lines import BASE_DUTY_BP
from synth.money import div_round_half_even

DATE_FMT = "%m/%d/%Y"
TXN_CODE = {"sale": "S", "return": "R", "credit": "C", "free_good": "F"}

# Revenue-side money columns expressed in the line's currency on export.
LINE_CURRENCY_MONEY_COLS = [
    "net_cents",
    "unit_list_cents",
    "list_value_cents",
    "discount_cents",
    "rebate_line_cents",
    "freight_concession_cents",
    "restock_fee_cents",
    "rtn_frt_billed_cents",
    "truth_settlement_rebate_cents",
    "truth_order_billed_cents",
]


def build_fx_rates(cfg: GeneratorConfig) -> pl.DataFrame:
    """Planted monthly fx path per non-USD currency: USD per local unit, as
    parts-per-million ints. Uses its own RNG stream (seed + 777) so enabling
    currencies never perturbs the main world stream."""
    rng = np.random.default_rng(cfg.seed + 777)
    starts = month_starts(cfg)
    # Returns lag up to two months past the sales horizon; cover those periods.
    tail = starts[-1]
    for _ in range(2):
        y, m = (tail.year, tail.month + 1) if tail.month < 12 else (tail.year + 1, 1)
        tail = tail.replace(year=y, month=m)
        starts = [*starts, tail]
    rows = []
    for cur in cfg.currency_shares:
        if cur == "USD":
            continue
        rate = cfg.fx_base.get(cur, 1.0)
        for d in starts:
            rate = rate * float(np.exp(rng.normal(0, cfg.fx_walk_sd)))
            rows.append(
                {
                    "currency": cur,
                    "period": d.strftime("%Y-%m"),
                    "rate_ppm": round(rate * 1_000_000),
                }
            )
    if not rows:
        return pl.DataFrame(
            schema={"currency": pl.String, "period": pl.String, "rate_ppm": pl.Int64}
        )
    return pl.DataFrame(rows)


def convert_lines_to_local(
    lines: pl.DataFrame, customers: pl.DataFrame, fx: pl.DataFrame
) -> pl.DataFrame:
    """Express revenue-side money of foreign customers in their currency:
    local_cents = half_even(usd_cents x 1e6 / rate_ppm). Costs stay USD."""
    lines = lines.join(customers.select("customer_id", "currency"), on="customer_id", how="left")
    if fx.height == 0:
        return lines
    lines = lines.with_columns(pl.col("invoice_date").dt.strftime("%Y-%m").alias("_period")).join(
        fx, left_on=["currency", "_period"], right_on=["currency", "period"], how="left"
    )
    rate = lines["rate_ppm"].fill_null(1_000_000).to_numpy()
    exprs = []
    for col in LINE_CURRENCY_MONEY_COLS:
        if col not in lines.columns:
            continue
        usd = lines[col].fill_null(0).to_numpy()
        # Keep the internal USD value for ground-truth totals; the exported
        # value is the local-currency amount.
        exprs.append(pl.Series(f"usd_{col}", usd))
        exprs.append(pl.Series(col, div_round_half_even(usd * 1_000_000, rate)))
    return lines.with_columns(exprs).drop("_period", "rate_ppm")


def _dollars(expr: pl.Expr, decimals: int = 2) -> pl.Expr:
    return (expr.cast(pl.Float64) / 100).round(decimals)


def export_erp_lines(lines: pl.DataFrame, out: Path) -> None:
    qty = lines["quantity"].to_numpy()
    net = lines["net_cents"].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        unit_prc = np.where(qty != 0, net / qty / 100.0, 0.0)
    cost = lines["unit_cost_cents"].to_numpy() / 100.0
    missing = lines["cost_missing"].to_numpy()
    cost_str = np.where(missing, "", np.char.mod("%.4f", cost))

    erp = lines.select(
        pl.col("line_key").alias("LINE_KEY"),
        pl.col("invoice_id").alias("INV_NO"),
        pl.col("invoice_date").dt.strftime(DATE_FMT).alias("INV_DT"),
        pl.col("order_id").alias("ORD_NO"),
        pl.col("customer_id").alias("CUST_NO"),
        pl.col("item_id").alias("ITEM_NO"),
        pl.col("quantity").alias("QTY_SHP"),
        pl.col("uom").alias("SELL_UOM"),
        pl.Series("UNIT_PRC", np.round(unit_prc, 4)),
        _dollars(pl.col("net_cents")).alias("EXT_NET"),
        _dollars(pl.col("unit_list_cents")).alias("LIST_PRC"),
        _dollars(pl.col("discount_cents")).alias("DISC_AMT"),
        pl.Series("UNIT_CST", cost_str),
        pl.col("txn_type").replace_strict(TXN_CODE).alias("TXN_CD"),
        pl.coalesce(pl.col("credit_reason"), pl.col("return_reason_code"), pl.lit("")).alias(
            "REASON_CD"
        ),
        pl.col("original_invoice_ref").fill_null("").alias("ORIG_INV"),
        pl.col("sales_rep_id").alias("REP_ID"),
        pl.col("channel").alias("CHNL"),
        _dollars(pl.col("freight_concession_cents")).alias("FRT_CONC"),
        _dollars(pl.col("restock_fee_cents").fill_null(0)).alias("RESTOCK_FEE"),
        _dollars(pl.col("rtn_frt_billed_cents").fill_null(0)).alias("RTN_FRT_BILLED"),
        _dollars(pl.col("rebate_line_cents")).alias("REB_AMT"),
        pl.col("rebate_program_id").fill_null("").alias("REB_PGM"),
        pl.col("contract_id").fill_null("").alias("CONTRACT_NO"),
        pl.col("kit_id").fill_null("").alias("KIT_ID"),
        pl.col("currency").fill_null("USD").alias("CURR"),
    )
    erp.write_csv(out / "invoice_lines.csv")


def export_reference_tables(
    cfg: GeneratorConfig,
    out: Path,
    customers: pl.DataFrame,
    items: pl.DataFrame,
    list_history: pl.DataFrame,
    uom_conversions: pl.DataFrame,
    contracts: pl.DataFrame,
    rebate_programs: pl.DataFrame,
    lines: pl.DataFrame,
    world: DemandWorld,
) -> None:
    starts = month_starts(cfg)
    periods = [d.strftime("%Y-%m") for d in starts]
    # Reference tables describe the real world: verified-duplicate copies are
    # export artifacts and must not inflate settlement or freight aggregates.
    lines = lines.unique(subset=["line_key"], keep="first", maintain_order=True)

    customers.select(
        pl.col("customer_id").alias("CUST_NO"),
        pl.col("industry").alias("INDUSTRY"),
        pl.col("region").alias("REGION"),
        pl.col("size_band").alias("SIZE_BAND"),
        pl.col("channel").alias("CHNL"),
        pl.col("contract_type").alias("CONTRACT_TYPE"),
        pl.col("bill_to_id").alias("BILL_TO"),
        pl.col("parent_account_id").alias("PARENT_ACCT"),
        pl.col("sales_rep_id").alias("REP_ID"),
    ).write_csv(out / "customers.csv")

    items.select(
        pl.col("item_id").alias("ITEM_NO"),
        pl.col("item_family").alias("FAMILY"),
        pl.col("item_category").alias("CATEGORY"),
        pl.col("supplier_id").alias("SUPPLIER"),
        pl.col("item_role").alias("ITEM_ROLE"),
        pl.col("is_service").cast(pl.Int8).alias("SVC_FLAG"),
        pl.col("base_uom").alias("BASE_UOM"),
        pl.col("weight_lb").alias("WEIGHT_LB"),
        pl.col("cube_ft3").alias("CUBE_FT"),
    ).write_csv(out / "items.csv")

    list_history.select(
        pl.col("item_id").alias("ITEM_NO"),
        pl.col("effective_date").dt.strftime(DATE_FMT).alias("EFF_DT"),
        _dollars(pl.col("list_cents")).alias("LIST_PRC"),
    ).write_csv(out / "price_list.csv")

    uom_conversions.select(
        pl.col("item_id").alias("ITEM_NO"),
        pl.col("from_uom").alias("FROM_UOM"),
        pl.col("to_uom").alias("TO_UOM"),
        pl.col("factor").alias("FACTOR"),
    ).write_csv(out / "uom_conversions.csv")

    if contracts.height:
        contracts.select(
            pl.col("contract_id").alias("CONTRACT_NO"),
            pl.col("customer_id").alias("CUST_NO"),
            pl.col("item_family").alias("FAMILY"),
            pl.col("start_date").dt.strftime(DATE_FMT).alias("START_DT"),
            pl.col("end_date").dt.strftime(DATE_FMT).alias("END_DT"),
            pl.col("discount_pct").alias("DISC_PCT"),
            pl.col("price_protected").cast(pl.Int8).alias("PROTECTED"),
        ).write_csv(out / "contracts.csv")

    rebate_programs.select(
        pl.col("rebate_program_id").alias("PGM_ID"),
        pl.col("customer_id").alias("CUST_NO"),
        pl.col("program_kind").alias("KIND"),
        (pl.col("rate_bp") / 100.0).alias("RATE_PCT"),
    ).write_csv(out / "rebate_programs.csv")

    # Settlement rebates: customer x quarter totals (planted as sum of line truth).
    settles = (
        lines.filter(pl.col("truth_settlement_rebate_cents") != 0)
        .with_columns(
            (
                pl.col("invoice_date").dt.year().cast(pl.String)
                + pl.lit("-Q")
                + pl.col("invoice_date").dt.quarter().cast(pl.String)
            ).alias("PERIOD")
        )
        .group_by("customer_id", "PERIOD", "rebate_program_id")
        .agg(pl.col("truth_settlement_rebate_cents").sum().alias("amt"))
        .sort("customer_id", "PERIOD")
    )
    settles.select(
        pl.col("customer_id").alias("CUST_NO"),
        pl.col("PERIOD"),
        pl.col("rebate_program_id").alias("PGM_ID"),
        _dollars(pl.col("amt")).alias("AMT"),
    ).write_csv(out / "rebate_settlements.csv")

    # Order-level billed freight and actual freight expense.
    orders = (
        lines.filter(pl.col("txn_type") == "sale")
        .group_by("order_id")
        .agg(
            pl.col("truth_order_billed_cents").first().alias("billed"),
            pl.col("truth_freight_expense_cents").sum().alias("exp"),
        )
        .sort("order_id")
    )
    orders.filter(pl.col("billed") > 0).select(
        pl.col("order_id").alias("ORD_NO"),
        pl.lit("FRT_OUT").alias("CHG_CD"),
        _dollars(pl.col("billed")).alias("AMT"),
    ).write_csv(out / "order_charges.csv")
    orders.select(
        pl.col("order_id").alias("ORD_NO"),
        _dollars(pl.col("exp")).alias("FRT_EXP_AMT"),
    ).write_csv(out / "freight_log.csv")

    # Return freight expense log: one row per return invoice.
    rtn = (
        lines.filter(pl.col("txn_type") == "return")
        .group_by("invoice_id")
        .agg(pl.col("truth_rtn_frt_exp_cents").sum().alias("exp"))
        .sort("invoice_id")
    )
    rtn.select(
        pl.col("invoice_id").alias("RET_INV_NO"),
        _dollars(pl.col("exp")).alias("RTN_FRT_EXP"),
    ).write_csv(out / "return_freight_log.csv")

    # Vendor rebate programs (supplier-level rate).
    vr = (
        lines.group_by("supplier_id")
        .agg(pl.col("vendor_rebate_bp").max().alias("bp"))
        .sort("supplier_id")
        .filter(pl.col("bp") > 0)
    )
    vr.select(
        pl.col("supplier_id").alias("SUPPLIER"),
        (pl.col("bp") / 100.0).alias("RATE_PCT"),
    ).write_csv(out / "vendor_rebate_programs.csv")

    # Instrument reference tables.
    sup_rows = [
        {
            "SUPPLIER": f"SUP{s:02d}",
            "PERIOD": periods[t],
            "COST_IDX": round(float(np.exp(world.z_cost[s, t])), 6),
        }
        for s in range(cfg.n_suppliers)
        for t in range(cfg.months)
    ]
    pl.DataFrame(sup_rows).write_csv(out / "supplier_cost_index.csv")
    tar_rows = [
        {
            "CATEGORY": f"CAT{g:02d}",
            "PERIOD": periods[t],
            "DUTY_PCT": round(
                BASE_DUTY_BP.get(g % 8, 0) * float(np.exp(world.z_tariff[g, t])) / 100.0, 4
            ),
        }
        for g in range(cfg.n_categories)
        for t in range(cfg.months)
    ]
    pl.DataFrame(tar_rows).write_csv(out / "tariff_schedule.csv")

    # Inventory receipts (Phase 14): monthly receipts covering ~115% of sold
    # units at the month's planted unit cost, plus an opening stock receipt.
    sold = (
        lines.filter(pl.col("txn_type") == "sale")
        .with_columns(pl.col("invoice_date").dt.strftime("%Y-%m").alias("_pm"))
        .group_by("item_id", "_pm")
        .agg(
            pl.col("quantity").sum().alias("q"),
            pl.col("unit_cost_cents").max().alias("c"),
        )
        .sort("item_id", "_pm")
    )
    receipts = sold.with_columns(
        ((pl.col("q") * 115).floordiv(100) + 1).alias("RCPT_QTY"),
        (pl.col("_pm") + "-01").alias("RCPT_DT"),
    )
    receipts.select(
        pl.col("item_id").alias("ITEM_NO"),
        pl.col("RCPT_DT"),
        pl.col("RCPT_QTY"),
        _dollars(pl.col("c"), 4).alias("UNIT_CST"),
    ).write_csv(out / "receipts.csv")

    # Landed-cost components per item-month present in the data.
    landed = (
        lines.filter(pl.col("txn_type") == "sale")
        .with_columns(pl.col("invoice_date").dt.strftime("%Y-%m").alias("PERIOD"))
        .group_by("item_id", "PERIOD")
        .agg(
            pl.col("inbound_freight_pu_cents").first().alias("inb"),
            pl.col("duty_pu_cents").first().alias("duty"),
        )
        .sort("item_id", "PERIOD")
    )
    landed.select(
        pl.col("item_id").alias("ITEM_NO"),
        pl.col("PERIOD"),
        _dollars(pl.col("inb"), 4).alias("INB_FRT_PU"),
        _dollars(pl.col("duty"), 4).alias("DUTY_PU"),
    ).write_csv(out / "landed_cost_components.csv")


def export_ground_truth(
    cfg: GeneratorConfig,
    out: Path,
    segments: list[SegmentDef],
    lines: pl.DataFrame,
    world: DemandWorld,
    customers: pl.DataFrame,
) -> dict[str, Any]:
    gt_dir = out / "ground_truth"
    gt_dir.mkdir(parents=True, exist_ok=True)

    line_truth = lines.select(
        "line_key",
        "txn_type",
        "segment_id",
        pl.col("truth_settlement_rebate_cents"),
        pl.col("rebate_line_cents").alias("truth_line_rebate_cents"),
        pl.col("truth_freight_expense_cents"),
        pl.col("truth_vendor_rebate_cents"),
        pl.col("freight_concession_cents").alias("truth_freight_concession_cents"),
        pl.col("truth_unit_cost_cents"),
        pl.col("cost_missing"),
        pl.col("truth_outlier_type"),
        pl.col("return_reason_code").alias("truth_return_reason_code"),
        pl.col("restock_fee_cents").alias("truth_restock_fee_cents"),
        pl.col("rtn_frt_billed_cents").alias("truth_rtn_frt_billed_cents"),
        pl.col("truth_rtn_frt_exp_cents"),
        pl.col("truth_tiered_accrual_cents"),
        pl.col("truth_tiered_rate_bp"),
        pl.col("truth_dup_type"),
        pl.col("truth_zero_qty"),
        pl.col("truth_uom_alt"),
        pl.col("inbound_freight_pu_cents"),
        pl.col("duty_pu_cents"),
        "quantity",
        "net_cents",
    ).unique(subset=["line_key"], keep="first", maintain_order=True)
    line_truth.write_parquet(gt_dir / "line_truth.parquet")

    panel_truth = world.panel.select(
        "customer_id",
        "item_id",
        "month",
        "segment_id",
        "active",
        "available",
        "bought",
        "qty",
        "unit_price_cents",
        "list_cents",
        "on_contract",
        "demand_shock",
        "z_cost",
        "z_tariff",
    )
    panel_truth.write_parquet(gt_dir / "panel_truth.parquet")

    # Economic truth is computed on the deduped frame: verified-duplicate
    # copies are export artifacts, not additional dollars.
    deduped = lines.unique(subset=["line_key"], keep="first", maintain_order=True)
    sales = deduped.filter(pl.col("txn_type") == "sale")
    mess_counts = {
        "returns": deduped.filter(pl.col("txn_type") == "return").height,
        "credits": deduped.filter(pl.col("txn_type") == "credit").height,
        "missing_cost": int(deduped["cost_missing"].sum()),
        "uom_alt": int(deduped["truth_uom_alt"].sum()),
        "verified_dup_keys": lines.filter(pl.col("truth_dup_type") == "verified_source")
        .select(pl.col("line_key").n_unique())
        .item(),
        "lookalike_dups": lines.filter(pl.col("truth_dup_type") == "lookalike_copy").height,
        "data_quality_outliers": sales.filter(
            pl.col("truth_outlier_type") == "data_quality"
        ).height,
        "commercial_outliers": sales.filter(pl.col("truth_outlier_type") == "commercial").height,
        "zero_qty_rows": int(deduped["truth_zero_qty"].sum()),
        "zero_cost_service_lines": sales.filter(
            pl.col("is_service") & ~pl.col("cost_missing")
        ).height,
    }

    def _usd(col: str) -> str:
        return f"usd_{col}" if f"usd_{col}" in deduped.columns else col

    totals = {
        "net_cents": int(deduped[_usd("net_cents")].sum()),
        "line_rebate_cents": int(deduped[_usd("rebate_line_cents")].sum()),
        "settlement_rebate_cents": int(deduped[_usd("truth_settlement_rebate_cents")].sum()),
        "freight_concession_cents": int(deduped[_usd("freight_concession_cents")].sum()),
        "freight_expense_cents": int(sales["truth_freight_expense_cents"].sum()),
        "billed_freight_cents": int(
            sales.group_by("order_id")
            .agg(pl.col("truth_order_billed_cents").first())
            .select(pl.col("truth_order_billed_cents").sum())
            .item()
        ),
        "vendor_rebate_cents": int(sales["truth_vendor_rebate_cents"].sum()),
    }
    summary: dict[str, Any] = {
        "generator_config": json.loads(cfg.model_dump_json()),
        "segments": [
            {
                "segment_id": s.segment_id,
                "name": s.name,
                "rule": s.rule,
                "elasticity": s.elasticity,
                "base_price_index": s.base_price_index,
            }
            for s in segments
        ],
        "naive_bias_direction": "toward_zero",
        "naive_bias_explanation": (
            "Reps discount when the demand shock is negative (endogeneity_gamma > 0), "
            "so realized price covaries positively with unobserved demand. OLS on "
            "realized prices is therefore biased upward, i.e. toward zero / less "
            "negative than the true elasticity."
        ),
        "mess_counts": mess_counts,
        "planted_totals_cents": totals,
        "n_lines_exported": lines.height,
        "n_customers": customers.height,
        "planted_small_order_customers": sorted(
            deduped.filter(
                pl.col("truth_small_order_customer")
                if "truth_small_order_customer" in deduped.columns
                else pl.lit(False)
            )["customer_id"]
            .unique()
            .to_list()
        ),
    }
    (gt_dir / "truth_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
