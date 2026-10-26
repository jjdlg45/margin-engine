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

Memory layout (Phase 35): each allocation is computed on a NARROW projection
(row id, group key, driver) instead of splitting the full-width ledger into
sale/other copies and re-concatenating. The narrow results are gathered into
one frame keyed by a unique row id (source_line_key can collide — collisions
are kept and flagged, so it is not a join key) and joined back in a single
pass; stage arithmetic is pure column expressions. The output columns, their
order, and every allocated cent are identical to the pre-refactor engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from margin_engine.config import Rules
from margin_engine.extended.tiered import TIERED_COLS, tiered_rebate_columns
from margin_engine.ingest.mapping import MappingError, SourceMapping
from margin_engine.money import div_half_even_expr
from margin_engine.waterfall.allocate import allocate_exact

_ROW = "_wf_row_id"


@dataclass
class WaterfallResult:
    lines: pl.DataFrame
    residuals: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Settlement rows that could not be attributed to any line (no lines in
    # the customer-period group, or an exactly-zero signed basis).
    unallocated_settlements: pl.DataFrame | None = None
    # Per customer x year x program tiered-rebate summary (Run 6, Phase 61):
    # the grain of the economic-event taxonomy and the rebate subledger.
    tiered_summary: pl.DataFrame | None = None


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
            pl.lit(None, dtype=pl.Date).alias("list_effective_date"),
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
        # Kept for provenance: guidance excludes floor-index comparisons when
        # the list in force is stale relative to the invoice date (24.4).
        pl.col("effective_date").alias("list_effective_date"),
    ).drop("_eff_list_scaled", "effective_date")


# Return-economics output columns, in ledger order (values then statuses).
_RETURN_COLS = [
    "return_recovery_cents",
    "return_handling_cents",
    "return_freight_exp_cents",
    "return_recovery_status",
    "return_freight_exp_status",
    "return_handling_status",
]


def _return_columns(
    rets: pl.DataFrame,
    mapping: SourceMapping,
    rules: Rules,
    return_freight_log: pl.DataFrame | None,
    notes: list[str],
    residuals: dict[str, int],
) -> pl.DataFrame:
    """Return recovery revenue and return costs (phase-8.2), computed on the
    narrow return-rows projection.

    Recovery (restocking fee + return freight billed to the customer) is
    ACTUAL when the export maps the columns, otherwise ESTIMATED from the
    reason policy. Return handling (per line + per unit) is always a config
    estimate. Return freight expense is actual from the return freight log
    (allocated within the return invoice, largest-remainder), else the config
    default per return invoice, labeled estimated. Handling and freight
    expense go to cost-to-serve; recovery adds to pocket revenue.
    """
    rc = rules.returns
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
    return rets.select(_ROW, *_RETURN_COLS)


def _kit_columns(kits: pl.DataFrame, notes: list[str], residuals: dict[str, int]) -> pl.DataFrame:
    """Kit revenue allocation (Phase 12) on the narrow kit-rows projection:
    within each (invoice, kit id) group, the priced parent line's revenue is
    reallocated to the zero-priced component lines by relative standalone
    price (effective list x normalized quantity), largest-remainder. The
    reallocation sums to zero per kit, so nothing is created or lost;
    kit_role labels parent vs component."""
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
        [
            parent.select(_ROW, "kit_revenue_realloc_cents", "kit_role"),
            comp.select(_ROW, "kit_revenue_realloc_cents", "kit_role"),
        ],
        how="vertical",
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
    is_ret = pl.col("transaction_type") == "return"

    # Ensure the return-economics input columns exist (same positions as the
    # pre-refactor engine: appended here when the mapping does not carry them).
    for col in ("restocking_fee_cents", "return_freight_billed_cents"):
        if col not in lines.columns:
            lines = lines.with_columns(pl.lit(None, dtype=pl.Int64).alias(col))
    if "return_reason" not in lines.columns:
        lines = lines.with_columns(
            pl.when(is_ret).then(pl.lit("unspecified")).otherwise(None).alias("return_reason")
        )

    # Unique row id: source_line_key is NOT unique (key collisions are kept and
    # flagged), so narrow allocation results join back on this instead.
    lines = lines.with_columns(pl.int_range(pl.len(), dtype=pl.Int64).alias(_ROW))
    col_order: list[str] = [c for c in lines.columns if c != _ROW]

    # Narrow per-allocation results accumulate here, keyed by row id.
    extra = lines.select(_ROW)

    def attach(narrow: pl.DataFrame) -> None:
        nonlocal extra
        extra = extra.join(narrow, on=_ROW, how="left", maintain_order="left")

    # --- return economics ---------------------------------------------------
    rets = lines.filter(is_ret).select(
        _ROW,
        "source_line_key",
        "invoice_id",
        "net_amount_cents",
        "quantity",
        "return_reason",
        "restocking_fee_cents",
        "return_freight_billed_cents",
    )
    if rets.height:
        attach(_return_columns(rets, mapping, rules, return_freight_log, notes, residuals))
    col_order.extend(_RETURN_COLS)

    # --- kit allocation -------------------------------------------------------
    if "kit_parent_id" in lines.columns:
        in_kit = pl.col("kit_parent_id").is_not_null() & sale
        kits = lines.filter(in_kit).select(
            _ROW,
            "source_line_key",
            "invoice_id",
            "kit_parent_id",
            "net_amount_cents",
            "eff_list_cents",
            "quantity_norm",
        )
        if kits.height:
            attach(_kit_columns(kits, notes, residuals))
    col_order.extend(["kit_revenue_realloc_cents", "kit_role"])

    # --- stage 2: billed charges ---------------------------------------------
    billed_ran = order_charges is not None and "order_id" in lines.columns
    if billed_ran and order_charges is not None:
        charges = order_charges.group_by("order_id").agg(
            pl.col("amount_cents").sum().alias("amount_cents_grp")
        )
        sl = lines.filter(sale).select(_ROW, "source_line_key", "order_id", "net_amount_cents")
        sl = allocate_exact(
            sl,
            charges,
            "order_id",
            "amount_cents_grp",
            "net_amount_cents",
            "billed_charge_alloc_cents",
        )
        attach(sl.select(_ROW, "billed_charge_alloc_cents"))
        total_charges = int(order_charges["amount_cents"].sum())
        allocated = int(sl["billed_charge_alloc_cents"].sum())
        residuals["billed_charges_unallocated_cents"] = total_charges - allocated
    col_order.extend(["billed_charge_alloc_cents", "billed_charge_status"])
    col_order.append("invoice_revenue_cents")
    col_order.append("rebate_actual_cents")
    if "freight_concession_cents" not in lines.columns:
        col_order.append("freight_concession_cents")
    if "discount_cents" not in lines.columns:
        col_order.append("discount_cents")

    # --- stage 3: settlement rebates -------------------------------------------
    # Allocation group: customer x quarter (settlement period) x program.
    unallocated_settlements: pl.DataFrame | None = None
    if rebate_settlements is not None:
        in_basis = (sale | is_ret) & (pl.col("net_amount_cents") != 0)
        bs = lines.filter(in_basis).select(
            _ROW, "source_line_key", "customer_id", "invoice_date", "net_amount_cents"
        )
        bs = bs.with_columns(
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
        bs = allocate_exact(
            bs,
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
            bs.group_by("_settle_key")
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
        attach(bs.select(_ROW, "rebate_settlement_alloc_cents"))
        total_settle = int(rebate_settlements["amount_cents"].sum())
        alloc_settle = int(bs["rebate_settlement_alloc_cents"].sum())
        residuals["settlement_rebates_unallocated_cents"] = total_settle - alloc_settle
        if total_settle - alloc_settle:
            notes.append(
                f"{unallocated.height} settlement row(s) have no matching lines in "
                f"their customer-period group; kept as visible residual"
            )
    col_order.extend(["rebate_settlement_alloc_cents", "rebate_settlement_status"])

    # Tiered retrospective rebates: accruals during the period (estimated),
    # settled customer-years trued up to actual; the waterfall uses whichever
    # is labeled in tiered_rebate_status.
    tiered_res = tiered_rebate_columns(
        lines.select(
            _ROW,
            "source_line_key",
            "customer_id",
            "invoice_date",
            "net_amount_cents",
            "transaction_type",
        ),
        rebate_programs,
        rebate_program_tiers,
        tiered_settlements,
        notes,
        residuals,
        row_col=_ROW,
    )
    if tiered_res is not None:
        attach(tiered_res.narrow)
    col_order.extend(TIERED_COLS)

    # Price credits: allocate each credit's amount over its original invoice's
    # sale lines by net share. A credit dollar counts exactly once: attributed
    # credits zero out on the credit row; unattributable credits keep their own
    # pocket effect and appear in the residual report.
    sale_invoice_ids = lines.filter(sale).select(pl.col("invoice_id").unique())
    # A mapping without original_invoice_ref cannot link credits to sales:
    # treat every ref as null (credit keeps its own pocket effect, residual).
    ref_expr = (
        pl.col("original_invoice_ref")
        if "original_invoice_ref" in lines.columns
        else pl.lit(None, dtype=pl.String).alias("original_invoice_ref")
    )
    credits = lines.filter(pl.col("credit_class") == "price_credit").select(
        pl.col("net_amount_cents"), ref_expr
    )
    if credits.height:
        linkable = credits.filter(
            pl.col("original_invoice_ref").is_in(sale_invoice_ids["invoice_id"].implode())
        )
        credit_by_inv = (
            linkable.group_by("original_invoice_ref")
            .agg(pl.col("net_amount_cents").sum().alias("credit_cents"))
            .rename({"original_invoice_ref": "invoice_id"})
        )
        sl = lines.filter(sale).select(_ROW, "source_line_key", "invoice_id", "net_amount_cents")
        sl = allocate_exact(
            sl,
            credit_by_inv,
            "invoice_id",
            "credit_cents",
            "net_amount_cents",
            "price_credit_alloc_cents",
        )
        attach(sl.select(_ROW, "price_credit_alloc_cents"))
        credit_attributed_expr = (pl.col("credit_class") == "price_credit") & ref_expr.is_in(
            sale_invoice_ids["invoice_id"].implode()
        )
        total_price_credit = int(credits["net_amount_cents"].sum())
        attributed_credit = int(linkable["net_amount_cents"].sum())
        residuals["price_credits_unattributed_cents"] = total_price_credit - attributed_credit
    else:
        credit_attributed_expr = pl.lit(False)
    col_order.extend(["price_credit_alloc_cents", "credit_attributed"])
    col_order.append("pocket_revenue_cents")

    # --- stage 6 allocations, computed narrow here so one join carries all ----
    # (their columns slot in after pocket_gp_cents; the final select orders them)
    freight_ran = freight_log is not None and "order_id" in lines.columns
    line_weight_added = False
    if freight_ran and freight_log is not None:
        # Weight driver preferred (spec allocation priority); fall back to net.
        if items is not None and "weight" in items.columns and "line_weight" not in lines.columns:
            lines = (
                lines.join(
                    items.select("item_id", pl.col("weight").alias("_wt")),
                    on="item_id",
                    how="left",
                    maintain_order="left",
                )
                .with_columns(
                    (pl.col("_wt").fill_null(0) * pl.col("quantity_norm").abs()).alias(
                        "line_weight"
                    )
                )
                .drop("_wt")
            )
            line_weight_added = True
        fl = freight_log.group_by("order_id").agg(pl.col("amount_cents").sum())
        driver = "line_weight" if "line_weight" in lines.columns else "net_amount_cents"
        sl = lines.filter(sale).select(_ROW, "source_line_key", "order_id", driver)
        sl = allocate_exact(
            sl, fl, "order_id", "amount_cents", driver, "freight_expense_alloc_cents"
        )
        attach(sl.select(_ROW, "freight_expense_alloc_cents"))
        total_fe = int(freight_log["amount_cents"].sum())
        alloc_fe = int(sl["freight_expense_alloc_cents"].sum())
        residuals["freight_expense_unallocated_cents"] = total_fe - alloc_fe

    # Non-price claims (cost credits) allocated to original invoice lines as
    # CTS. Supplier claims (Run 6, Phase 61) ride the same rails: their net is
    # positive (a recovery FROM the supplier), so the signed allocation reduces
    # cost-to-serve on the original lines; the counterparty distinction lives
    # in the economic-event taxonomy, never in customer revenue.
    cost_credits = lines.filter(
        pl.col("credit_class").is_in(["cost_credit", "supplier_claim"])
    ).select(pl.col("net_amount_cents"), ref_expr)
    if cost_credits.height:
        linkable_cc = cost_credits.filter(pl.col("original_invoice_ref").is_not_null())
        cc_by_inv = (
            linkable_cc.group_by("original_invoice_ref")
            .agg((-pl.col("net_amount_cents").sum()).alias("claim_cents"))
            .rename({"original_invoice_ref": "invoice_id"})
        )
        sl = lines.filter(sale).select(_ROW, "source_line_key", "invoice_id", "net_amount_cents")
        sl = allocate_exact(
            sl,
            cc_by_inv,
            "invoice_id",
            "claim_cents",
            "net_amount_cents",
            "claims_cost_alloc_cents",
        )
        attach(sl.select(_ROW, "claims_cost_alloc_cents"))
        total_claims = int(-cost_credits["net_amount_cents"].sum())
        alloc_claims = int(sl["claims_cost_alloc_cents"].sum())
        residuals["claims_unallocated_cents"] = total_claims - alloc_claims

    # --- one wide join brings every narrow allocation column back -------------
    if extra.width > 1:
        lines = lines.join(extra, on=_ROW, how="left", maintain_order="left")

    # Defaults for rows (or whole features) without an allocation: the
    # pre-refactor engine carried explicit zeros and null statuses.
    int_zero_cols = [
        "return_recovery_cents",
        "return_handling_cents",
        "return_freight_exp_cents",
        "kit_revenue_realloc_cents",
        "billed_charge_alloc_cents",
        "rebate_settlement_alloc_cents",
        *(c for c in TIERED_COLS if c != "tiered_rebate_status"),
        "price_credit_alloc_cents",
        "freight_expense_alloc_cents",
        "claims_cost_alloc_cents",
    ]
    str_null_cols = [
        "return_recovery_status",
        "return_freight_exp_status",
        "return_handling_status",
        "kit_role",
        "tiered_rebate_status",
    ]
    lines = lines.with_columns(
        *(
            pl.col(c).fill_null(0).alias(c)
            if c in lines.columns
            else pl.lit(0, dtype=pl.Int64).alias(c)
            for c in int_zero_cols
        ),
        *(pl.lit(None, dtype=pl.String).alias(c) for c in str_null_cols if c not in lines.columns),
        pl.when(sale)
        .then(pl.lit("allocated"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("billed_charge_status")
        if billed_ran
        else pl.lit(None, dtype=pl.String).alias("billed_charge_status"),
        pl.when(pl.col("rebate_settlement_alloc_cents").fill_null(0) != 0)
        .then(pl.lit("allocated"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("rebate_settlement_status")
        if rebate_settlements is not None
        else pl.lit(None, dtype=pl.String).alias("rebate_settlement_status"),
        pl.when(sale)
        .then(pl.lit("allocated"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("freight_expense_status")
        if freight_ran
        else pl.lit(None, dtype=pl.String).alias("freight_expense_status"),
    )

    # --- stage 2: invoice revenue = net + allocated billed charges ----------
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
        # Always present downstream (leakage, margin drivers): no mapped
        # on-invoice discount means zero declared discount.
        pl.col("discount_cents").fill_null(0).alias("discount_cents")
        if "discount_cents" in lines.columns
        else pl.lit(0, dtype=pl.Int64).alias("discount_cents"),
        credit_attributed_expr.alias("credit_attributed"),
    )

    is_credit = pl.col("transaction_type") == "credit"
    lines = lines.with_columns(
        pl.when(is_credit & pl.col("credit_attributed"))
        .then(0)  # attributed to original sale lines; counted there
        .when(is_credit & pl.col("credit_class").is_in(["cost_credit", "supplier_claim"]))
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
        if not mapping.cost_unavailable:
            msg = (
                "mapping declares no unit_cost; cost-dependent metrics cannot be "
                "computed. Add a unit_cost entry with cost_basis and inclusion "
                "flags — or, if the source genuinely has no cost, declare "
                "`cost_unavailable: true` in the mapping to state that "
                "explicitly (28.2). The engine never assumes it."
            )
            raise MappingError(msg)
        # Declared no-cost source: every line's product cost is null, every
        # cost-dependent metric downstream reports cost_unavailable.
        notes.append(
            "mapping declares cost_unavailable: product cost is null on every "
            "line; margin, floor and contribution outputs report "
            "cost_unavailable instead of numbers"
        )
        lines = lines.with_columns(pl.lit(None, dtype=pl.Int64).alias("base_cost_cents"))
        qty_scaled = pl.lit(0, dtype=pl.Int64)
    else:
        # Cost per declared unit basis: line UOM uses raw quantity; base UOM
        # uses the normalized quantity (e.g. 24 EA -> 2 CS, cost per case).
        qty_col = "quantity" if uc.per_uom == "line_uom" else "quantity_norm"
        qty_scaled = (pl.col(qty_col) * 10_000).round(0).cast(pl.Int64)
        base_cost = div_half_even_expr(pl.col("unit_cost_scaled") * qty_scaled, 10_000 * 100)
        lines = lines.with_columns(base_cost.alias("base_cost_cents"))
    col_order.append("base_cost_cents")

    addons = pl.lit(0, dtype=pl.Int64)
    if landed_cost_components is not None and uc is not None and not uc.includes_inbound_freight:
        lc = landed_cost_components.with_columns(
            pl.col("inbound_freight_pu_scaled").cast(pl.Int64),
            pl.col("duty_pu_scaled").cast(pl.Int64),
        ).rename({"period": "_period"})
        lines = (
            lines.with_columns(pl.col("invoice_date").dt.strftime("%Y-%m").alias("_period"))
            .join(lc, on=["item_id", "_period"], how="left", maintain_order="left")
            .drop("_period")
        )
        col_order.extend(c for c in lc.columns if c not in ("item_id", "_period"))
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
    col_order.append("landed_addon_cents")

    vendor_adj = pl.lit(0, dtype=pl.Int64)
    if (
        vendor_rebate_programs is not None
        and uc is not None
        and not uc.includes_vendor_rebates
        and items is not None
    ):
        if "supplier_id" not in lines.columns:
            lines = lines.join(
                items.select("item_id", "supplier_id"),
                on="item_id",
                how="left",
                maintain_order="left",
            )
            col_order.append("supplier_id")
        # Vendor consideration (Run 6, Phase 61 / ASC 705-20): a program reduces
        # acquisition cost by DEFAULT; programs declared as payment for a
        # distinct good or service, or reimbursement of identifiable selling
        # costs, never net into product cost — they are separate economic
        # events (ledger/events.py), so nothing is double counted or hidden.
        vr_src = vendor_rebate_programs
        if "consideration_type" in vr_src.columns:
            excluded = vr_src.filter(
                pl.col("consideration_type").is_not_null()
                & (pl.col("consideration_type") != "cost_reduction")
            )
            if excluded.height:
                notes.append(
                    f"{excluded.height} vendor program(s) declared as distinct-service "
                    f"or selling-cost reimbursement: excluded from product-cost "
                    f"netting (ASC 705-20 exception), reported as economic events"
                )
            vr_src = vr_src.filter(
                pl.col("consideration_type").is_null()
                | (pl.col("consideration_type") == "cost_reduction")
            )
        vr = vr_src.with_columns(
            (pl.col("rate_pct") * 100).round(0).cast(pl.Int64).alias("rate_bp")
        )
        lines = lines.join(
            vr.select("supplier_id", "rate_bp"), on="supplier_id", how="left", maintain_order="left"
        )
        col_order.append("rate_bp")
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
            if uc is not None
            else "cost_unavailable (declared in the mapping; no cost in the source)"
        ).alias("cost_build_label"),
    )
    col_order.extend(["vendor_rebate_cents", "product_cost_cents", "cost_build_label"])

    # --- stage 5: pocket gross profit ----------------------------------------
    lines = lines.with_columns(
        (pl.col("pocket_revenue_cents") - pl.col("product_cost_cents")).alias("pocket_gp_cents")
    )
    col_order.append("pocket_gp_cents")

    # --- stage 6: cost to serve (freight expense + non-price claims) ---------
    if line_weight_added:
        col_order.append("line_weight")
    col_order.extend(["freight_expense_alloc_cents", "freight_expense_status"])
    col_order.append("claims_cost_alloc_cents")
    lines = lines.with_columns(
        (
            pl.col("freight_expense_alloc_cents").fill_null(0)
            + pl.col("claims_cost_alloc_cents").fill_null(0)
            # Return handling and return freight expense are cost-to-serve.
            + pl.col("return_handling_cents").fill_null(0)
            + pl.col("return_freight_exp_cents").fill_null(0)
        ).alias("cost_to_serve_cents")
    )
    col_order.append("cost_to_serve_cents")

    # --- stage 7: contribution ------------------------------------------------
    lines = lines.with_columns(
        (pl.col("pocket_gp_cents") - pl.col("cost_to_serve_cents")).alias("contribution_cents")
    )
    col_order.append("contribution_cents")

    missing = [c for c in col_order if c not in lines.columns]
    unexpected = [c for c in lines.columns if c not in col_order and c != _ROW]
    if missing or unexpected:  # defensive: a logic error, never a data error
        msg = f"waterfall column accounting broke: missing={missing}, unexpected={unexpected}"
        raise RuntimeError(msg)
    lines = lines.select(col_order)
    return WaterfallResult(
        lines=lines,
        residuals=residuals,
        notes=notes,
        unallocated_settlements=unallocated_settlements,
        tiered_summary=tiered_res.summary if tiered_res is not None else None,
    )
