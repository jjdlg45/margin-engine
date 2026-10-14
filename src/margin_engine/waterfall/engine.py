"""Staged price waterfall with separate revenue and cost ledgers (spec 5.5).

Stages per line, all Int64 cents:
  1. gross_list_value_cents   effective-dated list x normalized quantity
  2. invoice_revenue_cents    net amount + allocated billed charges
  3. pocket_revenue_cents     - rebates (actual + allocated) - concessions - price credits
  4. product_cost_cents       mapped cost built up to landed, vendor rebates netted
  5. pocket_gp_cents          pocket revenue - product cost (null when cost missing)
  6. cost_to_serve_cents      allocated freight expense + non-price claims
  7. contribution_cents       pocket gp - cost to serve

Every allocated amount carries a *_status column: actual | allocated | estimated.
Nothing is forced: when an amount cannot be attributed, it stays as a visible
residual in the reconciliation report instead of being smeared over lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from margin_engine.ingest.mapping import MappingError, SourceMapping
from margin_engine.money import div_half_even_expr
from margin_engine.waterfall.allocate import allocate_exact


@dataclass
class WaterfallResult:
    lines: pl.DataFrame
    residuals: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def normalize_quantity(
    lines: pl.DataFrame, uom_conversions: pl.DataFrame | None, items: pl.DataFrame | None
) -> pl.DataFrame:
    """Normalize quantity to the item's base selling unit.

    A line whose UOM differs from the item base UOM needs a conversion row
    (from_uom = line uom, to_uom = base uom); missing conversions are an
    actionable error, never guessed.
    """
    if items is None or "base_uom" not in (items.columns if items is not None else []):
        return lines.with_columns(
            pl.col("quantity").alias("quantity_norm"),
            pl.col("uom").alias("uom_norm")
            if "uom" in lines.columns
            else pl.lit(None).alias("uom_norm"),
        )
    lines = lines.join(items.select("item_id", "base_uom"), on="item_id", how="left")
    if uom_conversions is not None:
        lines = lines.join(
            uom_conversions.rename({"from_uom": "uom", "to_uom": "base_uom"}),
            on=["item_id", "uom", "base_uom"],
            how="left",
        )
    else:
        lines = lines.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor"))
    needs = lines.filter(
        (pl.col("uom") != pl.col("base_uom"))
        & pl.col("factor").is_null()
        & pl.col("base_uom").is_not_null()
        & (pl.col("transaction_type") == "sale")
        & (pl.col("quantity") != 0)
    )
    if needs.height:
        pairs = needs.select("item_id", "uom", "base_uom").unique().head(10).to_dicts()
        msg = (
            f"{needs.height} line(s) are in a UOM with no conversion to the item's "
            f"base UOM, e.g. {pairs}. Add rows to the uom_conversions reference table."
        )
        raise MappingError(msg)
    return lines.with_columns(
        pl.when(pl.col("uom") == pl.col("base_uom"))
        .then(pl.col("quantity"))
        .when(pl.col("factor").is_not_null())
        .then(pl.col("quantity") / pl.col("factor"))
        .otherwise(pl.col("quantity"))
        .alias("quantity_norm"),
        pl.coalesce(pl.col("base_uom"), pl.col("uom")).alias("uom_norm"),
    ).drop("factor")


def effective_list(lines: pl.DataFrame, price_list: pl.DataFrame | None) -> pl.DataFrame:
    """Gross list value from the effective-dated price list x normalized quantity.

    Exact integer math: list_cents x quantity scaled by 1e4, half-even back.
    Lines without an effective list keep null -> stage labeled incomplete.
    List price is NEVER manufactured from the invoice price.
    """
    if price_list is None:
        return lines.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("eff_list_cents"),
            pl.lit(None, dtype=pl.Int64).alias("gross_list_value_cents"),
        )
    pl_sorted = price_list.sort("item_id", "effective_date").with_columns(
        pl.col("list_price_scaled").alias("_eff_list_scaled")
    )
    joined = lines.sort("item_id", "invoice_date").join_asof(
        pl_sorted.select("item_id", "effective_date", "_eff_list_scaled"),
        left_on="invoice_date",
        right_on="effective_date",
        by="item_id",
        strategy="backward",
        check_sortedness=False,  # sorted by (item_id, date) immediately above
    )
    return joined.with_columns(
        div_half_even_expr(pl.col("_eff_list_scaled"), 100).alias("eff_list_cents"),
        # list_scaled (1e-4 dollars) x qty_scaled (1e-4 units) = 1e-8 dollars;
        # cents = value / 1e6.
        div_half_even_expr(
            (
                pl.col("_eff_list_scaled")
                * (pl.col("quantity_norm") * 10_000).round(0).cast(pl.Int64)
            ),
            1_000_000,
        ).alias("gross_list_value_cents"),
    ).drop("_eff_list_scaled", "effective_date")


def build_waterfall(
    ledger: pl.DataFrame,
    mapping: SourceMapping,
    *,
    price_list: pl.DataFrame | None,
    uom_conversions: pl.DataFrame | None,
    items: pl.DataFrame | None,
    order_charges: pl.DataFrame | None,
    freight_log: pl.DataFrame | None,
    rebate_settlements: pl.DataFrame | None,
    rebate_programs: pl.DataFrame | None,
    vendor_rebate_programs: pl.DataFrame | None,
    landed_cost_components: pl.DataFrame | None,
) -> WaterfallResult:
    notes: list[str] = []
    residuals: dict[str, int] = {}
    lines = normalize_quantity(ledger, uom_conversions, items)
    lines = effective_list(lines, price_list)
    if price_list is None:
        notes.append("no price_list reference: gross list stage labeled incomplete")

    sale = pl.col("transaction_type") == "sale"

    # --- stage 2: invoice revenue = net + allocated billed charges ----------
    if order_charges is not None and "order_id" in lines.columns:
        charges = order_charges.group_by("order_id").agg(
            pl.col("amount_cents").sum().alias("charge_cents")
        )
        # Allocate by net amount share across the order's sale lines.
        sale_lines = lines.filter(sale)
        other = lines.filter(~sale)
        sale_lines = allocate_exact(
            sale_lines,
            charges.rename({"charge_cents": "amount_cents_grp"}),
            "order_id",
            "amount_cents_grp",
            "net_amount_cents",
            "billed_charge_alloc_cents",
        )
        lines = pl.concat(
            [
                sale_lines.with_columns(pl.lit("allocated").alias("billed_charge_status")),
                other.with_columns(
                    pl.lit(0, dtype=pl.Int64).alias("billed_charge_alloc_cents"),
                    pl.lit(None, dtype=pl.String).alias("billed_charge_status"),
                ),
            ],
            how="vertical_relaxed",
        )
        total_charges = int(order_charges["amount_cents"].sum())
        allocated = int(lines["billed_charge_alloc_cents"].fill_null(0).sum())
        residuals["billed_charges_unallocated_cents"] = total_charges - allocated
    else:
        lines = lines.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("billed_charge_alloc_cents"),
            pl.lit(None, dtype=pl.String).alias("billed_charge_status"),
        )
    lines = lines.with_columns(
        (pl.col("net_amount_cents") + pl.col("billed_charge_alloc_cents")).alias(
            "invoice_revenue_cents"
        )
    )

    # --- stage 3: pocket revenue -------------------------------------------
    # Line-accrued rebates are actual; settlement rebates are allocated over the
    # customer-quarter's sale net; price credits allocated over original invoice.
    lines = lines.with_columns(
        pl.col("rebate_cents").fill_null(0).alias("rebate_actual_cents")
        if "rebate_cents" in lines.columns
        else pl.lit(0, dtype=pl.Int64).alias("rebate_actual_cents"),
        pl.col("freight_concession_cents").fill_null(0).alias("freight_concession_cents")
        if "freight_concession_cents" in lines.columns
        else pl.lit(0, dtype=pl.Int64).alias("freight_concession_cents"),
    )

    if rebate_settlements is not None:
        # Allocation group: customer x quarter (settlement period) x program.
        lines = lines.with_columns(
            (
                pl.col("customer_id")
                + "|"
                + pl.col("invoice_date").dt.year().cast(pl.String)
                + "-Q"
                + pl.col("invoice_date").dt.quarter().cast(pl.String)
            ).alias("_settle_key")
        )
        settle = (
            rebate_settlements.with_columns(
                (pl.col("customer_id") + "|" + pl.col("period")).alias("_settle_key")
            )
            .group_by("_settle_key")
            .agg(pl.col("amount_cents").sum())
        )
        # Settlements accrue on sales net of returns: allocate over both, with
        # signed weights so return lines carry negative rebate shares.
        in_basis = (sale | (pl.col("transaction_type") == "return")) & (
            pl.col("net_amount_cents") != 0
        )
        sale_lines = lines.filter(in_basis)
        other = lines.filter(~in_basis)
        sale_lines = allocate_exact(
            sale_lines,
            settle,
            "_settle_key",
            "amount_cents",
            "net_amount_cents",
            "rebate_settlement_alloc_cents",
            signed_driver=True,
        )
        lines = pl.concat(
            [
                sale_lines.with_columns(
                    pl.when(pl.col("rebate_settlement_alloc_cents") != 0)
                    .then(pl.lit("allocated"))
                    .otherwise(pl.lit(None, dtype=pl.String))
                    .alias("rebate_settlement_status")
                ),
                other.with_columns(
                    pl.lit(0, dtype=pl.Int64).alias("rebate_settlement_alloc_cents"),
                    pl.lit(None, dtype=pl.String).alias("rebate_settlement_status"),
                ),
            ],
            how="vertical_relaxed",
        ).drop("_settle_key")
        total_settle = int(rebate_settlements["amount_cents"].sum())
        alloc_settle = int(lines["rebate_settlement_alloc_cents"].fill_null(0).sum())
        residuals["settlement_rebates_unallocated_cents"] = total_settle - alloc_settle
        if total_settle - alloc_settle:
            notes.append(
                "part of settlement rebates could not be allocated (no matching "
                "customer-period sale lines); kept as visible residual"
            )
    else:
        lines = lines.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("rebate_settlement_alloc_cents"),
            pl.lit(None, dtype=pl.String).alias("rebate_settlement_status"),
        )

    # Price credits: allocate each credit's amount over its original invoice's
    # sale lines by net share. A credit dollar counts exactly once: attributed
    # credits zero out on the credit row; unattributable credits keep their own
    # pocket effect and appear in the residual report.
    sale_invoice_ids = lines.filter(sale).select(pl.col("invoice_id").unique())
    credits = lines.filter(pl.col("credit_class") == "price_credit")
    if credits.height:
        linkable = credits.filter(
            pl.col("original_invoice_ref").is_in(sale_invoice_ids["invoice_id"].implode())
        )
        credit_by_inv = (
            linkable.group_by("original_invoice_ref")
            .agg(pl.col("net_amount_cents").sum().alias("credit_cents"))
            .rename({"original_invoice_ref": "invoice_id"})
        )
        sale_lines = lines.filter(sale)
        other = lines.filter(~sale)
        sale_lines = allocate_exact(
            sale_lines,
            credit_by_inv,
            "invoice_id",
            "credit_cents",
            "net_amount_cents",
            "price_credit_alloc_cents",
        )
        lines = pl.concat(
            [
                sale_lines,
                other.with_columns(pl.lit(0, dtype=pl.Int64).alias("price_credit_alloc_cents")),
            ],
            how="vertical_relaxed",
        )
        lines = lines.with_columns(
            (
                (pl.col("credit_class") == "price_credit")
                & pl.col("original_invoice_ref").is_in(sale_invoice_ids["invoice_id"].implode())
            ).alias("credit_attributed")
        )
        total_price_credit = int(credits["net_amount_cents"].sum())
        attributed_credit = int(linkable["net_amount_cents"].sum())
        residuals["price_credits_unattributed_cents"] = total_price_credit - attributed_credit
    else:
        lines = lines.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("price_credit_alloc_cents"),
            pl.lit(False).alias("credit_attributed"),
        )

    is_credit = pl.col("transaction_type") == "credit"
    lines = lines.with_columns(
        pl.when(is_credit & pl.col("credit_attributed"))
        .then(0)  # attributed to original sale lines; counted there
        .when(is_credit & (pl.col("credit_class") == "cost_credit"))
        .then(0)  # claims belong to the cost side, allocated below
        .otherwise(
            pl.col("invoice_revenue_cents")
            - pl.col("rebate_actual_cents")
            - pl.col("rebate_settlement_alloc_cents")
            - pl.col("freight_concession_cents")
            + pl.when(sale).then(pl.col("price_credit_alloc_cents")).otherwise(0)
        )
        .alias("pocket_revenue_cents")
    )

    # --- stage 4: product cost (landed build-up per declared semantics) -----
    uc = mapping.lines.unit_cost
    if uc is None:
        msg = (
            "mapping declares no unit_cost; cost-dependent metrics cannot be "
            "computed. Add a unit_cost entry with cost_basis and inclusion flags."
        )
        raise MappingError(msg)
    # Cost per declared unit basis: line UOM uses raw quantity; base UOM uses
    # the normalized quantity (e.g. 24 EA -> 2 CS when cost is per case).
    qty_col = "quantity" if uc.per_uom == "line_uom" else "quantity_norm"
    qty_scaled = (pl.col(qty_col) * 10_000).round(0).cast(pl.Int64)
    base_cost = div_half_even_expr(pl.col("unit_cost_scaled") * qty_scaled, 10_000 * 100)
    lines = lines.with_columns(base_cost.alias("base_cost_cents"))

    addons = pl.lit(0, dtype=pl.Int64)
    if landed_cost_components is not None and not uc.includes_inbound_freight:
        lc = landed_cost_components.with_columns(
            pl.col("inbound_freight_pu_scaled").cast(pl.Int64),
            pl.col("duty_pu_scaled").cast(pl.Int64),
        )
        lines = (
            lines.with_columns(pl.col("invoice_date").dt.strftime("%Y-%m").alias("_period"))
            .join(
                lc.rename({"period": "_period"}),
                on=["item_id", "_period"],
                how="left",
            )
            .drop("_period")
        )
        inb = div_half_even_expr(
            pl.col("inbound_freight_pu_scaled").fill_null(0) * qty_scaled, 10_000 * 100
        )
        duty = (
            div_half_even_expr(pl.col("duty_pu_scaled").fill_null(0) * qty_scaled, 10_000 * 100)
            if not uc.includes_duty
            else pl.lit(0, dtype=pl.Int64)
        )
        addons = inb + duty
    lines = lines.with_columns(addons.alias("landed_addon_cents"))

    vendor_adj = pl.lit(0, dtype=pl.Int64)
    if vendor_rebate_programs is not None and not uc.includes_vendor_rebates and items is not None:
        if "supplier_id" not in lines.columns:
            lines = lines.join(items.select("item_id", "supplier_id"), on="item_id", how="left")
        vr = vendor_rebate_programs.with_columns(
            (pl.col("rate_pct") * 100).round(0).cast(pl.Int64).alias("rate_bp")
        )
        lines = lines.join(vr.select("supplier_id", "rate_bp"), on="supplier_id", how="left")
        # Vendor rebates apply to the purchase (base) cost, not freight or duty.
        vendor_adj = div_half_even_expr(
            pl.col("base_cost_cents") * pl.col("rate_bp").fill_null(0), 10_000
        )
    lines = lines.with_columns(vendor_adj.alias("vendor_rebate_cents"))
    lines = lines.with_columns(
        pl.when(pl.col("unit_cost_scaled").is_null())
        .then(None)
        .otherwise(
            pl.col("base_cost_cents") + pl.col("landed_addon_cents") - pl.col("vendor_rebate_cents")
        )
        .alias("product_cost_cents"),
        pl.lit(
            f"basis={uc.cost_basis.value};inbound={'incl' if uc.includes_inbound_freight else 'added'};"
            f"duty={'incl' if uc.includes_duty else 'added'};"
            f"vendor_rebates={'incl' if uc.includes_vendor_rebates else 'netted'}"
        ).alias("cost_build_label"),
    )

    # --- stage 5: pocket gross profit ----------------------------------------
    lines = lines.with_columns(
        (pl.col("pocket_revenue_cents") - pl.col("product_cost_cents")).alias("pocket_gp_cents")
    )

    # --- stage 6: cost to serve (freight expense + non-price claims) ---------
    if freight_log is not None and "order_id" in lines.columns:
        fl = freight_log.group_by("order_id").agg(pl.col("amount_cents").sum())
        sale_lines = lines.filter(sale)
        other = lines.filter(~sale)
        # Weight driver preferred (spec allocation priority); fall back to net.
        driver = "line_weight" if "line_weight" in sale_lines.columns else "net_amount_cents"
        sale_lines = allocate_exact(
            sale_lines, fl, "order_id", "amount_cents", driver, "freight_expense_alloc_cents"
        )
        lines = pl.concat(
            [
                sale_lines.with_columns(pl.lit("allocated").alias("freight_expense_status")),
                other.with_columns(
                    pl.lit(0, dtype=pl.Int64).alias("freight_expense_alloc_cents"),
                    pl.lit(None, dtype=pl.String).alias("freight_expense_status"),
                ),
            ],
            how="vertical_relaxed",
        )
        total_fe = int(freight_log["amount_cents"].sum())
        alloc_fe = int(lines["freight_expense_alloc_cents"].fill_null(0).sum())
        residuals["freight_expense_unallocated_cents"] = total_fe - alloc_fe
    else:
        lines = lines.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("freight_expense_alloc_cents"),
            pl.lit(None, dtype=pl.String).alias("freight_expense_status"),
        )

    # Non-price claims (cost credits) allocated to original invoice lines as CTS.
    cost_credits = lines.filter(pl.col("credit_class") == "cost_credit")
    if cost_credits.height:
        linkable = cost_credits.filter(pl.col("original_invoice_ref").is_not_null())
        cc_by_inv = (
            linkable.group_by("original_invoice_ref")
            .agg((-pl.col("net_amount_cents").sum()).alias("claim_cents"))
            .rename({"original_invoice_ref": "invoice_id"})
        )
        sale_lines = lines.filter(sale)
        other = lines.filter(~sale)
        sale_lines = allocate_exact(
            sale_lines,
            cc_by_inv,
            "invoice_id",
            "claim_cents",
            "net_amount_cents",
            "claims_cost_alloc_cents",
        )
        lines = pl.concat(
            [
                sale_lines,
                other.with_columns(pl.lit(0, dtype=pl.Int64).alias("claims_cost_alloc_cents")),
            ],
            how="vertical_relaxed",
        )
        total_claims = int(-cost_credits["net_amount_cents"].sum())
        alloc_claims = int(lines.filter(sale)["claims_cost_alloc_cents"].sum())
        residuals["claims_unallocated_cents"] = total_claims - alloc_claims
    else:
        lines = lines.with_columns(pl.lit(0, dtype=pl.Int64).alias("claims_cost_alloc_cents"))

    lines = lines.with_columns(
        (
            pl.col("freight_expense_alloc_cents").fill_null(0)
            + pl.col("claims_cost_alloc_cents").fill_null(0)
        ).alias("cost_to_serve_cents")
    )

    # --- stage 7: contribution ------------------------------------------------
    lines = lines.with_columns(
        (pl.col("pocket_gp_cents") - pl.col("cost_to_serve_cents")).alias("contribution_cents")
    )
    return WaterfallResult(lines=lines, residuals=residuals, notes=notes)
