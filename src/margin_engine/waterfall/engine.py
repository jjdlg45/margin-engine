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

from margin_engine.config import Rules
from margin_engine.extended.tiered import apply_tiered_rebates
from margin_engine.ingest.mapping import MappingError, SourceMapping
from margin_engine.money import div_half_even_expr
from margin_engine.waterfall.allocate import allocate_exact


@dataclass
class WaterfallResult:
    lines: pl.DataFrame
    residuals: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Settlement rows that could not be attributed to any line (no lines in
    # the customer-period group, or an exactly-zero signed basis).
    unallocated_settlements: pl.DataFrame | None = None


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


def _return_economics(
    lines: pl.DataFrame,
    mapping: SourceMapping,
    rules: Rules,
    return_freight_log: pl.DataFrame | None,
    notes: list[str],
    residuals: dict[str, int],
) -> pl.DataFrame:
    """Return recovery revenue and return costs (phase-8.2).

    Recovery (restocking fee + return freight billed to the customer) is
    ACTUAL when the export maps the columns, otherwise ESTIMATED from the
    reason policy. Return handling (per line + per unit) is always a config
    estimate. Return freight expense is actual from the return freight log
    (allocated within the return invoice, largest-remainder), else the config
    default per return invoice, labeled estimated. Handling and freight
    expense go to cost-to-serve; recovery adds to pocket revenue.
    """
    rc = rules.returns
    is_ret = pl.col("transaction_type") == "return"
    for col in ("restocking_fee_cents", "return_freight_billed_cents"):
        if col not in lines.columns:
            lines = lines.with_columns(pl.lit(None, dtype=pl.Int64).alias(col))
    if "return_reason" not in lines.columns:
        lines = lines.with_columns(
            pl.when(is_ret).then(pl.lit("unspecified")).otherwise(None).alias("return_reason")
        )
    zero_cols = [
        pl.lit(0, dtype=pl.Int64).alias("return_recovery_cents"),
        pl.lit(0, dtype=pl.Int64).alias("return_handling_cents"),
        pl.lit(0, dtype=pl.Int64).alias("return_freight_exp_cents"),
        pl.lit(None, dtype=pl.String).alias("return_recovery_status"),
        pl.lit(None, dtype=pl.String).alias("return_freight_exp_status"),
        pl.lit(None, dtype=pl.String).alias("return_handling_status"),
    ]
    rets = lines.filter(is_ret)
    if rets.height == 0:
        return lines.with_columns(zero_cols)
    other = lines.filter(~is_ret).with_columns(zero_cols)

    fee_bp_map = {
        reason: int(policy.restocking_fee_pct * 10_000)
        for reason, policy in rc.reason_policies.items()
    }
    customer_pays_map = {
        reason: policy.return_freight_paid_by == "customer"
        for reason, policy in rc.reason_policies.items()
    }
    fee_bp = pl.col("return_reason").replace_strict(fee_bp_map, default=0, return_dtype=pl.Int64)
    customer_pays = pl.col("return_reason").replace_strict(
        customer_pays_map, default=False, return_dtype=pl.Boolean
    )

    # --- return freight expense ------------------------------------------
    if return_freight_log is not None:
        log = return_freight_log.group_by("invoice_id").agg(pl.col("amount_cents").sum())
        rets = allocate_exact(
            rets,
            log,
            "invoice_id",
            "amount_cents",
            "net_amount_cents",
            "return_freight_exp_cents",
        )
        covered = pl.col("invoice_id").is_in(log["invoice_id"].implode())
        rets = rets.with_columns(
            pl.when(covered)
            .then(pl.lit("actual"))
            .otherwise(pl.lit("estimated"))
            .alias("return_freight_exp_status")
        )
        log_total = int(log["amount_cents"].sum())
        allocated = int(rets["return_freight_exp_cents"].sum())
        residuals["return_freight_unallocated_cents"] = log_total - allocated
    else:
        rets = rets.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("return_freight_exp_cents"),
            pl.lit("estimated").alias("return_freight_exp_status"),
        )
        notes.append(
            "no return_freight_log reference: return freight expense estimated from config defaults"
        )
    # Lines whose invoice is absent from the log get the config default,
    # split equally across the return invoice's lines.
    rets = rets.with_columns(
        pl.when(pl.col("return_freight_exp_status") == "estimated")
        .then(
            div_half_even_expr(
                pl.lit(rc.default_return_freight_per_return_cents, dtype=pl.Int64),
                pl.len().over("invoice_id").cast(pl.Int64),
            )
        )
        .otherwise(pl.col("return_freight_exp_cents"))
        .alias("return_freight_exp_cents")
    )

    # --- handling ----------------------------------------------------------
    # Today handling always comes from config defaults (status `estimated`);
    # a future source of actual handling charges would set `actual` here.
    rets = rets.with_columns(
        (
            pl.lit(rc.default_handling_cost_per_return_cents, dtype=pl.Int64)
            + pl.lit(rc.default_handling_cost_per_unit_cents, dtype=pl.Int64)
            * pl.col("quantity").abs().round(0).cast(pl.Int64)
        ).alias("return_handling_cents"),
        pl.lit("estimated").alias("return_handling_status"),
    )

    # --- recovery revenue --------------------------------------------------
    fee_mapped = mapping.lines.restocking_fee is not None
    billed_mapped = mapping.lines.return_freight_billed is not None
    fee = (
        pl.col("restocking_fee_cents").fill_null(0)
        if fee_mapped
        else div_half_even_expr((-pl.col("net_amount_cents")) * fee_bp, 10_000)
    )
    billed = (
        pl.col("return_freight_billed_cents").fill_null(0)
        if billed_mapped
        else pl.when(customer_pays).then(pl.col("return_freight_exp_cents")).otherwise(0)
    )
    rets = rets.with_columns(
        (fee + billed).alias("return_recovery_cents"),
        pl.lit("actual" if fee_mapped and billed_mapped else "estimated").alias(
            "return_recovery_status"
        ),
    )
    if not (fee_mapped and billed_mapped):
        notes.append(
            "restocking fee / return freight billed not in the export: return "
            "recovery estimated from the reason policy (labeled estimated)"
        )
    return pl.concat([other, rets], how="diagonal_relaxed")


def _kit_allocation(
    lines: pl.DataFrame, notes: list[str], residuals: dict[str, int]
) -> pl.DataFrame:
    """Kit revenue allocation (Phase 12): within each (invoice, kit id) group,
    the priced parent line's revenue is reallocated to the zero-priced
    component lines by relative standalone price (effective list x normalized
    quantity), largest-remainder. The reallocation sums to zero per kit, so
    nothing is created or lost; kit_role labels parent vs component."""
    if "kit_parent_id" not in lines.columns:
        return lines.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("kit_revenue_realloc_cents"),
            pl.lit(None, dtype=pl.String).alias("kit_role"),
        )
    in_kit = pl.col("kit_parent_id").is_not_null() & (pl.col("transaction_type") == "sale")
    kits = lines.filter(in_kit)
    other = lines.filter(~in_kit).with_columns(
        pl.lit(0, dtype=pl.Int64).alias("kit_revenue_realloc_cents"),
        pl.lit(None, dtype=pl.String).alias("kit_role"),
    )
    if kits.height == 0:
        return other
    kits = kits.with_columns(
        (pl.col("invoice_id") + "|" + pl.col("kit_parent_id")).alias("_kit_key"),
        (pl.col("net_amount_cents") > 0).alias("_is_parent"),
    )
    # Standalone value driver for components; parents excluded from allocation.
    comp = kits.filter(~pl.col("_is_parent")).with_columns(
        (
            pl.col("eff_list_cents").fill_null(0)
            * (pl.col("quantity_norm") * 10_000).round(0).cast(pl.Int64)
        )
        .floordiv(10_000)
        .alias("_standalone")
    )
    parent = kits.filter(pl.col("_is_parent"))
    kit_revenue = parent.group_by("_kit_key").agg(pl.col("net_amount_cents").sum().alias("kit_rev"))
    comp = allocate_exact(
        comp, kit_revenue, "_kit_key", "kit_rev", "_standalone", "kit_revenue_realloc_cents"
    ).drop("_standalone")
    parent = parent.with_columns(
        (-pl.col("net_amount_cents")).alias("kit_revenue_realloc_cents"),
        pl.lit("kit_parent").alias("kit_role"),
    )
    comp = comp.with_columns(pl.lit("kit_component").alias("kit_role"))
    # Conservation: reallocation nets to zero per kit (parents with no
    # components keep their revenue; their reallocation is reversed).
    comp_sum = dict(
        comp.group_by("_kit_key").agg(pl.col("kit_revenue_realloc_cents").sum()).iter_rows()
    )
    parent = parent.with_columns(
        pl.when(pl.col("_kit_key").replace_strict(comp_sum, default=0, return_dtype=pl.Int64) == 0)
        .then(0)
        .otherwise(pl.col("kit_revenue_realloc_cents"))
        .alias("kit_revenue_realloc_cents")
    )
    out = pl.concat(
        [other, parent.drop("_is_parent", "_kit_key"), comp.drop("_is_parent", "_kit_key")],
        how="diagonal_relaxed",
    )
    residuals["kit_realloc_net_cents"] = int(out["kit_revenue_realloc_cents"].sum())
    if residuals["kit_realloc_net_cents"] != 0:
        notes.append("kit reallocation did not net to zero — investigate")
    return out


def build_waterfall(
    ledger: pl.DataFrame,
    mapping: SourceMapping,
    *,
    rules: Rules,
    price_list: pl.DataFrame | None,
    uom_conversions: pl.DataFrame | None,
    items: pl.DataFrame | None,
    order_charges: pl.DataFrame | None,
    freight_log: pl.DataFrame | None,
    rebate_settlements: pl.DataFrame | None,
    rebate_programs: pl.DataFrame | None,
    vendor_rebate_programs: pl.DataFrame | None,
    landed_cost_components: pl.DataFrame | None,
    return_freight_log: pl.DataFrame | None = None,
    rebate_program_tiers: pl.DataFrame | None = None,
    tiered_settlements: pl.DataFrame | None = None,
) -> WaterfallResult:
    notes: list[str] = []
    residuals: dict[str, int] = {}
    lines = normalize_quantity(ledger, uom_conversions, items)
    lines = effective_list(lines, price_list)
    if price_list is None:
        notes.append("no price_list reference: gross list stage labeled incomplete")

    sale = pl.col("transaction_type") == "sale"
    lines = _return_economics(lines, mapping, rules, return_freight_log, notes, residuals)
    lines = _kit_allocation(lines, notes, residuals)

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
        (
            pl.col("net_amount_cents")
            + pl.col("billed_charge_alloc_cents")
            + pl.col("kit_revenue_realloc_cents")
        ).alias("invoice_revenue_cents")
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
        # A settlement is allocated whenever its group's total signed weight is
        # nonzero (return-only groups allocate too). Residual arises ONLY when
        # no lines match the group (or the signed total is exactly zero); those
        # settlements are listed individually for the report.
        alloc_by_group = dict(
            sale_lines.group_by("_settle_key")
            .agg(pl.col("rebate_settlement_alloc_cents").sum())
            .iter_rows()
        )
        unallocated = (
            rebate_settlements.with_columns(
                (pl.col("customer_id") + "|" + pl.col("period")).alias("_settle_key")
            )
            .with_columns(
                pl.col("_settle_key")
                .replace_strict(alloc_by_group, default=0, return_dtype=pl.Int64)
                .alias("_allocated")
            )
            .filter((pl.col("_allocated") == 0) & (pl.col("amount_cents") != 0))
            .drop("_settle_key", "_allocated")
        )
        unallocated_settlements = unallocated
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
                f"{unallocated.height} settlement row(s) have no matching lines in "
                f"their customer-period group; kept as visible residual"
            )
    else:
        unallocated_settlements = None
        lines = lines.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("rebate_settlement_alloc_cents"),
            pl.lit(None, dtype=pl.String).alias("rebate_settlement_status"),
        )

    # Tiered retrospective rebates: accruals during the period (estimated),
    # settled customer-years trued up to actual; the waterfall uses whichever
    # is labeled in tiered_rebate_status.
    lines = apply_tiered_rebates(
        lines, rebate_programs, rebate_program_tiers, tiered_settlements, notes, residuals
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
            - pl.col("tiered_rebate_used_cents")
            + pl.when(sale).then(pl.col("price_credit_alloc_cents")).otherwise(0)
            # Return recovery (restocking fee + return freight billed to the
            # customer) is revenue; zero on non-return lines.
            + pl.col("return_recovery_cents")
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
        # Weight driver preferred (spec allocation priority); fall back to net.
        if items is not None and "weight" in items.columns and "line_weight" not in lines.columns:
            lines = (
                lines.join(
                    items.select("item_id", pl.col("weight").alias("_wt")), on="item_id", how="left"
                )
                .with_columns(
                    (pl.col("_wt").fill_null(0) * pl.col("quantity_norm").abs()).alias(
                        "line_weight"
                    )
                )
                .drop("_wt")
            )
        sale_lines = lines.filter(sale)
        other = lines.filter(~sale)
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
            # Return handling and return freight expense are cost-to-serve.
            + pl.col("return_handling_cents").fill_null(0)
            + pl.col("return_freight_exp_cents").fill_null(0)
        ).alias("cost_to_serve_cents")
    )

    # --- stage 7: contribution ------------------------------------------------
    lines = lines.with_columns(
        (pl.col("pocket_gp_cents") - pl.col("cost_to_serve_cents")).alias("contribution_cents")
    )
    return WaterfallResult(
        lines=lines,
        residuals=residuals,
        notes=notes,
        unallocated_settlements=unallocated_settlements,
    )
