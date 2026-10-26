"""Economic event taxonomy (Run 6, Phase 61).

Every adjustment in the data is classified by its ECONOMICS, independently of
raw ERP labels (the mapping normalizes source codes to canonical reasons
first; this module classifies the canonical data):

- customer_price_concession      discounts, rebates, concessions, price credits
- customer_refund_return         returns of product
- customer_service_payment       restocking fees, return freight billed
- non_price_customer_claim       damage / shortage / quality / warranty credits
- supplier_rebate_allowance      vendor consideration netting acquisition cost
- supplier_quality_claim         recoveries from a supplier (configured reasons)
- vendor_distinct_service_reimbursement   ASC 705-20 exception: payment for a
                                 distinct good or service (never nets cost)
- vendor_selling_cost_reimbursement       ASC 705-20 exception: reimbursement
                                 of identifiable selling costs
- freight_pass_through           billed charges the config declares as such
- tax_deposit_liability          taxes / deposits / third-party collections
- other_adjustment               everything not yet classified (stated, never
                                 silently absorbed)

Every event carries: counterparty, direction, source document and reference,
original invoice link, program, earn and settlement periods, eligible basis,
estimate-vs-actual status, a reversal/true-up link, and a duplicate-suspect
flag. A credit memo that appears both embedded on its original invoice and as
a standalone credit document is counted ONCE in event totals: the standalone
event is kept but marked `duplicate_suspect`, and the excluded amount is
reported. The ledger itself keeps every source dollar either way — events are
an analytical classification, reconciliation stays untouched.

Vendor rebate roll-forward (ASC 705-20): rebates earned on RECEIPTS versus
rebates recognized against the cost of goods SOLD; the balance belongs to
inventory still on hand and must not hit current-period line cost. Computed
only when an inventory-receipts table exists; otherwise reported as not
testable, never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from margin_engine.config import Rules
from margin_engine.ingest.mapping import ChargeDecl
from margin_engine.money import div_half_even_expr

EVENT_TYPES = (
    "customer_price_concession",
    "customer_refund_return",
    "customer_service_payment",
    "non_price_customer_claim",
    "supplier_rebate_allowance",
    "supplier_quality_claim",
    "vendor_distinct_service_reimbursement",
    "vendor_selling_cost_reimbursement",
    "freight_pass_through",
    "tax_deposit_liability",
    "other_adjustment",
)

EVENTS_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "event_id": pl.String,
    "event_type": pl.String,
    "event_subtype": pl.String,
    "counterparty": pl.String,
    "direction": pl.String,
    "amount_cents": pl.Int64,
    "amount_status": pl.String,
    "source_document": pl.String,
    "source_ref": pl.String,
    "original_invoice_ref": pl.String,
    "program_id": pl.String,
    "earn_period": pl.String,
    "settlement_period": pl.String,
    "eligible_basis_cents": pl.Int64,
    "reversal_link": pl.String,
    "duplicate_suspect": pl.Boolean,
    "customer_id": pl.String,
    "supplier_id": pl.String,
    "item_id": pl.String,
}

_ROW = "_ev_row_id"


@dataclass
class EventsResult:
    events: pl.DataFrame
    totals: pl.DataFrame  # per event_type: n_events, amount (duplicates excluded)
    duplicate_excluded_cents: int
    notes: list[str] = field(default_factory=list)


def _s(col: str, frame: pl.DataFrame) -> pl.Expr:
    """String column if present, else null — optional ledger columns."""
    if col in frame.columns:
        return pl.col(col).cast(pl.String)
    return pl.lit(None, dtype=pl.String)


def _finish(frame: pl.DataFrame, id_prefix: str) -> pl.DataFrame:
    """Fill missing schema columns with nulls/defaults and order them."""
    out = frame
    defaults: dict[str, pl.Expr] = {
        "event_subtype": pl.lit(None, dtype=pl.String),
        "original_invoice_ref": pl.lit(None, dtype=pl.String),
        "program_id": pl.lit(None, dtype=pl.String),
        "settlement_period": pl.lit(None, dtype=pl.String),
        "eligible_basis_cents": pl.lit(None, dtype=pl.Int64),
        "reversal_link": pl.lit(None, dtype=pl.String),
        "duplicate_suspect": pl.lit(value=False),
        "customer_id": pl.lit(None, dtype=pl.String),
        "supplier_id": pl.lit(None, dtype=pl.String),
        "item_id": pl.lit(None, dtype=pl.String),
        "earn_period": pl.lit(None, dtype=pl.String),
    }
    for name, expr in defaults.items():
        if name not in out.columns:
            out = out.with_columns(expr.alias(name))
    out = out.with_columns(
        pl.concat_str([pl.lit(id_prefix + "|"), pl.col("source_ref").fill_null("")]).alias(
            "event_id"
        )
    )
    return out.select(list(EVENTS_SCHEMA)).cast(EVENTS_SCHEMA)  # type: ignore[arg-type]


def _month(col: str = "invoice_date") -> pl.Expr:
    return pl.col(col).dt.strftime("%Y-%m")


def _line_family(
    led: pl.DataFrame,
    predicate: pl.Expr,
    *,
    id_prefix: str,
    event_type: str,
    subtype: str,
    counterparty: str,
    direction: str,
    amount: pl.Expr,
    status: pl.Expr | str,
    program: pl.Expr | None = None,
    reversal: pl.Expr | None = None,
    basis: pl.Expr | None = None,
    dup: pl.Expr | None = None,
) -> pl.DataFrame:
    """One event per ledger line matching the predicate, vectorized."""
    rows = led.filter(predicate)
    if rows.height == 0:
        return pl.DataFrame(schema=EVENTS_SCHEMA)
    status_expr = pl.lit(status) if isinstance(status, str) else status
    out = rows.select(
        pl.lit(event_type).alias("event_type"),
        pl.lit(subtype).alias("event_subtype"),
        pl.lit(counterparty).alias("counterparty"),
        pl.lit(direction).alias("direction"),
        amount.cast(pl.Int64).alias("amount_cents"),
        status_expr.cast(pl.String).alias("amount_status"),
        pl.lit("invoice_line").alias("source_document"),
        pl.concat_str(
            [pl.col("source_line_key").cast(pl.String), pl.lit("#"), pl.col(_ROW).cast(pl.String)]
        ).alias("source_ref"),
        _s("original_invoice_ref", rows).alias("original_invoice_ref"),
        (program if program is not None else pl.lit(None, dtype=pl.String)).alias("program_id"),
        _month().alias("earn_period"),
        (basis if basis is not None else pl.col("net_amount_cents"))
        .cast(pl.Int64)
        .alias("eligible_basis_cents"),
        (reversal if reversal is not None else pl.lit(None, dtype=pl.String)).alias(
            "reversal_link"
        ),
        (dup if dup is not None else pl.lit(value=False)).alias("duplicate_suspect"),
        _s("customer_id", rows).alias("customer_id"),
        _s("supplier_id", rows).alias("supplier_id"),
        _s("item_id", rows).alias("item_id"),
    )
    return _finish(out, id_prefix)


def build_events(
    ledger: pl.DataFrame,
    *,
    rules: Rules,
    rebate_settlements: pl.DataFrame | None = None,
    order_charges: pl.DataFrame | None = None,
    vendor_rebate_programs: pl.DataFrame | None = None,
    tiered_summary: pl.DataFrame | None = None,
    charge_declarations: dict[str, ChargeDecl] | None = None,
) -> EventsResult:
    """Classify every adjustment in the ledger into economic events.

    `charge_declarations` is the mapping's charge-semantics table (Run 6,
    Phase 63): a declared pool drives the event type (pass-through revenue ->
    freight pass-through; excluded liability -> tax/deposit/collection), with
    the config's charge_event_types as the fallback for undeclared codes."""
    notes: list[str] = []
    led = ledger.with_columns(pl.int_range(pl.len(), dtype=pl.Int64).alias(_ROW))
    sale = pl.col("transaction_type") == "sale"
    is_ret = pl.col("transaction_type") == "return"
    is_credit = pl.col("transaction_type") == "credit"
    dup_flag = (
        pl.col("flag_duplicate_adjustment_suspect")
        if "flag_duplicate_adjustment_suspect" in led.columns
        else pl.lit(value=False)
    )
    program = _s("rebate_program_id", led)

    frames: list[pl.DataFrame] = [
        # --- customer price concessions -------------------------------------
        _line_family(
            led,
            sale & (pl.col("discount_cents") > 0),
            id_prefix="disc",
            event_type="customer_price_concession",
            subtype="on_invoice_discount",
            counterparty="customer",
            direction="revenue_reduction",
            amount=pl.col("discount_cents"),
            status="actual",
        ),
        _line_family(
            led,
            pl.col("rebate_actual_cents") != 0,
            id_prefix="reb",
            event_type="customer_price_concession",
            subtype="customer_rebate_line",
            counterparty="customer",
            direction="revenue_reduction",
            amount=pl.col("rebate_actual_cents"),
            status="actual",
            program=program,
        ),
        _line_family(
            led,
            pl.col("freight_concession_cents") != 0,
            id_prefix="conc",
            event_type="customer_price_concession",
            subtype="freight_concession",
            counterparty="customer",
            direction="revenue_reduction",
            amount=pl.col("freight_concession_cents"),
            status="actual",
        ),
        _line_family(
            led,
            is_credit & (pl.col("credit_class") == "price_credit"),
            id_prefix="pcr",
            event_type="customer_price_concession",
            subtype="price_credit_memo",
            counterparty="customer",
            direction="revenue_reduction",
            amount=-pl.col("net_amount_cents"),
            status="actual",
            reversal=_s("original_invoice_ref", led),
            dup=dup_flag,
        ),
        # --- returns and their service payments ------------------------------
        _line_family(
            led,
            is_ret,
            id_prefix="ret",
            event_type="customer_refund_return",
            subtype=("return"),
            counterparty="customer",
            direction="revenue_reduction",
            amount=-pl.col("net_amount_cents"),
            status="actual",
            reversal=_s("original_invoice_ref", led),
        ),
        _line_family(
            led,
            is_ret & (pl.col("return_recovery_cents") != 0),
            id_prefix="rrec",
            event_type="customer_service_payment",
            subtype="return_recovery",
            counterparty="customer",
            direction="revenue_increase",
            amount=pl.col("return_recovery_cents"),
            status=pl.col("return_recovery_status").cast(pl.String),
        ),
        # --- claims -----------------------------------------------------------
        _line_family(
            led,
            is_credit & (pl.col("credit_class") == "cost_credit"),
            id_prefix="clm",
            event_type="non_price_customer_claim",
            subtype="customer_claim",
            counterparty="customer",
            direction="cost_increase",
            amount=-pl.col("net_amount_cents"),
            status="actual",
            reversal=_s("original_invoice_ref", led),
            dup=dup_flag,
        ),
        _line_family(
            led,
            is_credit & (pl.col("credit_class") == "supplier_claim"),
            id_prefix="sclm",
            event_type="supplier_quality_claim",
            subtype="supplier_recovery",
            counterparty="supplier",
            direction="cost_reduction",
            amount=pl.col("net_amount_cents"),
            status="actual",
            reversal=_s("original_invoice_ref", led),
        ),
        _line_family(
            led,
            is_credit & (pl.col("credit_class") == "unclassified_credit"),
            id_prefix="ucr",
            event_type="other_adjustment",
            subtype="unclassified_credit",
            counterparty="customer",
            direction="revenue_reduction",
            amount=-pl.col("net_amount_cents"),
            status="actual",
            dup=dup_flag,
        ),
    ]
    if led.filter(is_credit & (pl.col("credit_class") == "unclassified_credit")).height:
        notes.append(
            "credit memos with reasons outside the configured taxonomy are "
            "reported as other adjustments: add their canonical reasons to the "
            "price, cost or supplier-claim reason lists in the transactions "
            "section of rules.yaml to classify them"
        )

    # --- settlement rebates (document grain) --------------------------------
    if rebate_settlements is not None and rebate_settlements.height:
        qbasis = (
            led.filter((sale | is_ret) & (pl.col("net_amount_cents") != 0))
            .with_columns(
                (
                    pl.col("invoice_date").dt.year().cast(pl.String)
                    + "-Q"
                    + pl.col("invoice_date").dt.quarter().cast(pl.String)
                ).alias("period")
            )
            .group_by("customer_id", "period")
            .agg(pl.col("net_amount_cents").sum().alias("eligible_basis_cents"))
        )
        settle = (
            rebate_settlements.with_columns(pl.int_range(pl.len(), dtype=pl.Int64).alias(_ROW))
            .join(
                qbasis.with_columns(pl.col("customer_id").cast(pl.String)),
                on=["customer_id", "period"],
                how="left",
            )
            .select(
                pl.lit("customer_price_concession").alias("event_type"),
                pl.lit("rebate_settlement").alias("event_subtype"),
                pl.lit("customer").alias("counterparty"),
                pl.lit("revenue_reduction").alias("direction"),
                pl.col("amount_cents").cast(pl.Int64),
                pl.when(pl.col("eligible_basis_cents").is_not_null())
                .then(pl.lit("allocated"))
                .otherwise(pl.lit("unallocated_residual"))
                .alias("amount_status"),
                pl.lit("rebate_settlement_row").alias("source_document"),
                pl.concat_str(
                    [
                        pl.col("customer_id").cast(pl.String),
                        pl.lit("|"),
                        pl.col("period").cast(pl.String),
                        pl.lit("#"),
                        pl.col(_ROW).cast(pl.String),
                    ]
                ).alias("source_ref"),
                _s("rebate_program_id", rebate_settlements).alias("program_id"),
                pl.col("period").cast(pl.String).alias("earn_period"),
                pl.col("period").cast(pl.String).alias("settlement_period"),
                pl.col("eligible_basis_cents").cast(pl.Int64),
                pl.col("customer_id").cast(pl.String),
            )
        )
        frames.append(_finish(settle, "rset"))

    # --- tiered rebate lifecycle (customer x year x program grain) -----------
    if tiered_summary is not None and tiered_summary.height:
        acc = tiered_summary.select(
            pl.lit("customer_price_concession").alias("event_type"),
            pl.lit("tiered_rebate_accrual").alias("event_subtype"),
            pl.lit("customer").alias("counterparty"),
            pl.lit("revenue_reduction").alias("direction"),
            pl.col("accrued_cents").cast(pl.Int64).alias("amount_cents"),
            pl.when(pl.col("negative_basis") & ~pl.col("settled") & (pl.col("accrued_cents") == 0))
            .then(pl.lit("fail_closed_negative_basis"))
            .otherwise(pl.lit("estimated"))
            .alias("amount_status"),
            pl.lit("tiered_program_accrual").alias("source_document"),
            pl.concat_str(
                [
                    pl.col("customer_id"),
                    pl.lit("|"),
                    pl.col("year"),
                    pl.lit("|"),
                    pl.col("rebate_program_id"),
                ]
            ).alias("source_ref"),
            pl.col("rebate_program_id").alias("program_id"),
            pl.col("year").alias("earn_period"),
            pl.col("eligible_basis_cents").cast(pl.Int64),
            pl.col("customer_id"),
        )
        frames.append(_finish(acc, "tacc"))
        settled = tiered_summary.filter(pl.col("settled"))
        if settled.height:
            tset = settled.select(
                pl.lit("customer_price_concession").alias("event_type"),
                pl.lit("tiered_rebate_settlement").alias("event_subtype"),
                pl.lit("customer").alias("counterparty"),
                pl.lit("revenue_reduction").alias("direction"),
                pl.col("actual_alloc_cents").cast(pl.Int64).alias("amount_cents"),
                pl.lit("actual").alias("amount_status"),
                pl.lit("tiered_settlement_row").alias("source_document"),
                pl.concat_str(
                    [
                        pl.col("customer_id"),
                        pl.lit("|"),
                        pl.col("year"),
                        pl.lit("|"),
                        pl.col("rebate_program_id"),
                    ]
                ).alias("source_ref"),
                pl.col("rebate_program_id").alias("program_id"),
                pl.col("year").alias("earn_period"),
                pl.col("year").alias("settlement_period"),
                pl.col("eligible_basis_cents").cast(pl.Int64),
                # The settlement REPLACES the matching accrual estimate.
                pl.concat_str(
                    [
                        pl.lit("tacc|"),
                        pl.col("customer_id"),
                        pl.lit("|"),
                        pl.col("year"),
                        pl.lit("|"),
                        pl.col("rebate_program_id"),
                    ]
                ).alias("reversal_link"),
                pl.col("customer_id"),
            )
            frames.append(_finish(tset, "tset"))

    # --- vendor consideration (supplier x month grain) ------------------------
    if "vendor_rebate_cents" in led.columns and "supplier_id" in led.columns:
        vend = (
            led.filter(sale & (pl.col("vendor_rebate_cents") != 0))
            .group_by("supplier_id", _month().alias("earn_period"))
            .agg(
                pl.col("vendor_rebate_cents").sum().alias("amount_cents"),
                pl.col("base_cost_cents").sum().alias("eligible_basis_cents"),
            )
            .select(
                pl.lit("supplier_rebate_allowance").alias("event_type"),
                pl.lit("vendor_rebate_cost_netting").alias("event_subtype"),
                pl.lit("supplier").alias("counterparty"),
                pl.lit("cost_reduction").alias("direction"),
                pl.col("amount_cents").cast(pl.Int64),
                pl.lit("allocated").alias("amount_status"),
                pl.lit("vendor_rebate_program").alias("source_document"),
                pl.concat_str(
                    [pl.col("supplier_id").cast(pl.String), pl.lit("|"), pl.col("earn_period")]
                ).alias("source_ref"),
                pl.col("earn_period"),
                pl.col("eligible_basis_cents").cast(pl.Int64),
                pl.col("supplier_id").cast(pl.String),
            )
        )
        if vend.height:
            frames.append(_finish(vend, "vreb"))

    # ASC 705-20 exceptions: programs that never net cost become their own
    # events, estimated on the same sold-cost basis the netting would have used.
    if (
        vendor_rebate_programs is not None
        and "consideration_type" in vendor_rebate_programs.columns
        and "supplier_id" in led.columns
        and "base_cost_cents" in led.columns
    ):
        exc = vendor_rebate_programs.filter(
            pl.col("consideration_type").is_not_null()
            & (pl.col("consideration_type") != "cost_reduction")
        )
        if exc.height:
            base = (
                led.filter(sale & pl.col("base_cost_cents").is_not_null())
                .group_by("supplier_id", _month().alias("earn_period"))
                .agg(pl.col("base_cost_cents").sum().alias("eligible_basis_cents"))
                .with_columns(pl.col("supplier_id").cast(pl.String))
            )
            exc_rows = exc.with_columns(
                (pl.col("rate_pct") * 100).round(0).cast(pl.Int64).alias("rate_bp")
            ).join(base, on="supplier_id", how="inner")
            exc_events = exc_rows.select(
                pl.when(pl.col("consideration_type") == "distinct_service")
                .then(pl.lit("vendor_distinct_service_reimbursement"))
                .otherwise(pl.lit("vendor_selling_cost_reimbursement"))
                .alias("event_type"),
                pl.col("consideration_type").cast(pl.String).alias("event_subtype"),
                pl.lit("supplier").alias("counterparty"),
                pl.when(pl.col("consideration_type") == "distinct_service")
                .then(pl.lit("other_income"))
                .otherwise(pl.lit("cost_reduction"))
                .alias("direction"),
                div_half_even_expr(
                    pl.col("eligible_basis_cents") * pl.col("rate_bp"), 10_000
                ).alias("amount_cents"),
                pl.lit("estimated").alias("amount_status"),
                pl.lit("vendor_rebate_program").alias("source_document"),
                pl.concat_str([pl.col("supplier_id"), pl.lit("|"), pl.col("earn_period")]).alias(
                    "source_ref"
                ),
                pl.col("earn_period"),
                pl.col("eligible_basis_cents").cast(pl.Int64),
                pl.col("supplier_id"),
            )
            if exc_events.height:
                frames.append(_finish(exc_events, "vexc"))
                notes.append(
                    "vendor programs declared as distinct-service or "
                    "selling-cost reimbursement are reported as economic "
                    "events, not cost netting (ASC 705-20 exceptions); their "
                    "amounts are rate-based estimates"
                )

    # --- taxes / deposits / third-party collections (Run 6, Phase 63) ---------
    if "tax_collected_cents" in led.columns:
        frames.append(
            _line_family(
                led,
                pl.col("tax_collected_cents") != 0,
                id_prefix="tax",
                event_type="tax_deposit_liability",
                subtype="tax_collected",
                counterparty="tax_authority",
                direction="liability_collection",
                amount=pl.col("tax_collected_cents"),
                status="actual",
            )
        )

    # --- billed order charges -------------------------------------------------
    if order_charges is not None and order_charges.height:
        type_map = dict(rules.transactions.charge_event_types)
        decls = charge_declarations or {}
        for code_key, decl in decls.items():
            if decl.pool == "pass_through_revenue":
                type_map[code_key] = "freight_pass_through"
            elif decl.pool == "excluded_liability":
                type_map[code_key] = "tax_deposit_liability"
        owner_map = {k: d.economic_owner for k, d in decls.items()}
        direction_map = {k: str(d.direction) for k, d in decls.items()}
        order_month = (
            led.filter(sale)
            .group_by("order_id")
            .agg(_month().min().alias("earn_period"))
            .with_columns(pl.col("order_id").cast(pl.String))
            if "order_id" in led.columns
            else None
        )
        oc = order_charges.with_columns(pl.int_range(pl.len(), dtype=pl.Int64).alias(_ROW))
        if order_month is not None:
            oc = oc.with_columns(pl.col("order_id").cast(pl.String)).join(
                order_month, on="order_id", how="left"
            )
        else:
            oc = oc.with_columns(pl.lit(None, dtype=pl.String).alias("earn_period"))
        code = _s("charge_code", oc)
        bad = sorted(set(type_map.values()) - set(EVENT_TYPES))
        if bad:
            msg = (
                f"transactions.charge_event_types maps to unknown event types "
                f"{bad}; valid types: {sorted(EVENT_TYPES)}"
            )
            raise ValueError(msg)
        oc_events = oc.select(
            code.replace_strict(type_map, default="other_adjustment").alias("event_type"),
            pl.concat_str([pl.lit("billed_order_charge:"), code.fill_null("unknown")]).alias(
                "event_subtype"
            ),
            code.replace_strict(owner_map, default="customer").alias("counterparty")
            if owner_map
            else pl.lit("customer").alias("counterparty"),
            code.replace_strict(direction_map, default="revenue_increase").alias("direction")
            if direction_map
            else pl.lit("revenue_increase").alias("direction"),
            pl.col("amount_cents").cast(pl.Int64),
            pl.lit("actual").alias("amount_status"),
            pl.lit("order_charge_row").alias("source_document"),
            pl.concat_str(
                [pl.col("order_id").cast(pl.String), pl.lit("#"), pl.col(_ROW).cast(pl.String)]
            ).alias("source_ref"),
            pl.col("earn_period"),
        )
        frames.append(_finish(oc_events, "chg"))
        declared = sorted(set(type_map) | set(decls))
        unmapped = oc.filter(~code.is_in(declared) if declared else pl.lit(value=True))
        if unmapped.height:
            notes.append(
                f"{unmapped.height} billed order charge(s) have no declared "
                "economic classification and are reported as other adjustments "
                "- declare their charge codes in the transactions section of "
                "rules.yaml (the engine never guesses what a charge code means)"
            )

    events = (
        pl.concat([f for f in frames if f.height > 0], how="vertical")
        if any(f.height for f in frames)
        else pl.DataFrame(schema=EVENTS_SCHEMA)
    )
    events = events.sort(["event_type", "event_subtype", "source_ref"])

    dup_excluded = int(
        events.filter(pl.col("duplicate_suspect"))["amount_cents"].sum() if events.height else 0
    )
    if dup_excluded:
        notes.append(
            f"duplicate adjustment representations excluded from event totals: "
            f"{dup_excluded} cents (rows kept and flagged; the ledger still "
            f"reconciles every source dollar)"
        )
    totals = (
        events.filter(~pl.col("duplicate_suspect"))
        .group_by("event_type")
        .agg(pl.len().alias("n_events"), pl.col("amount_cents").sum())
        .sort("event_type")
    )
    return EventsResult(
        events=events, totals=totals, duplicate_excluded_cents=dup_excluded, notes=notes
    )


# ---------------------------------------------------------------------------
# Vendor rebate roll-forward (ASC 705-20)
# ---------------------------------------------------------------------------

ROLLFORWARD_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "supplier_id": pl.String,
    "period": pl.String,
    "earned_on_receipts_cents": pl.Int64,
    "recognized_in_cogs_cents": pl.Int64,
    "period_change_cents": pl.Int64,
    "balance_on_inventory_cents": pl.Int64,
    "status": pl.String,
}


@dataclass
class RollforwardResult:
    table: pl.DataFrame | None
    notes: list[str] = field(default_factory=list)


def vendor_rebate_rollforward(
    ledger: pl.DataFrame,
    receipts: pl.DataFrame | None,
    items: pl.DataFrame | None,
    vendor_rebate_programs: pl.DataFrame | None,
) -> RollforwardResult:
    """Earned (receipts x rate) vs recognized (vendor netting on sold cost);
    the running balance is the rebate attributable to inventory still on hand,
    which must never hit current-period line cost. Estimated: the recognized
    side nets at the declared cost basis, which can differ from receipt cost.
    """
    if vendor_rebate_programs is None:
        return RollforwardResult(table=None, notes=["no vendor rebate programs: roll-forward off"])
    if receipts is None or items is None:
        return RollforwardResult(
            table=None,
            notes=[
                "vendor rebate roll-forward not testable: no inventory receipts "
                "(or items) table, so rebates earned on inventory still on hand "
                "cannot be separated from rebates recognized in cost of goods sold"
            ],
        )
    vr = vendor_rebate_programs
    if "consideration_type" in vr.columns:
        vr = vr.filter(
            pl.col("consideration_type").is_null()
            | (pl.col("consideration_type") == "cost_reduction")
        )
    vr = vr.with_columns((pl.col("rate_pct") * 100).round(0).cast(pl.Int64).alias("rate_bp"))
    earned = (
        receipts.join(items.select("item_id", "supplier_id"), on="item_id", how="left")
        .join(vr.select("supplier_id", "rate_bp"), on="supplier_id", how="inner")
        .with_columns(
            pl.col("receipt_date").dt.strftime("%Y-%m").alias("period"),
            div_half_even_expr(
                pl.col("unit_cost_cents") * pl.col("quantity") * pl.col("rate_bp"), 10_000
            ).alias("earned_cents"),
        )
        .group_by("supplier_id", "period")
        .agg(pl.col("earned_cents").sum().alias("earned_on_receipts_cents"))
    )
    if "vendor_rebate_cents" not in ledger.columns or "supplier_id" not in ledger.columns:
        return RollforwardResult(
            table=None,
            notes=["vendor rebate roll-forward not testable: ledger carries no vendor netting"],
        )
    recognized = (
        ledger.filter((pl.col("transaction_type") == "sale") & (pl.col("vendor_rebate_cents") != 0))
        .group_by("supplier_id", pl.col("invoice_date").dt.strftime("%Y-%m").alias("period"))
        .agg(pl.col("vendor_rebate_cents").sum().alias("recognized_in_cogs_cents"))
        .with_columns(pl.col("supplier_id").cast(pl.String))
    )
    table = (
        earned.with_columns(pl.col("supplier_id").cast(pl.String))
        .join(recognized, on=["supplier_id", "period"], how="full", coalesce=True)
        .with_columns(
            pl.col("earned_on_receipts_cents").fill_null(0),
            pl.col("recognized_in_cogs_cents").fill_null(0),
        )
        .with_columns(
            (pl.col("earned_on_receipts_cents") - pl.col("recognized_in_cogs_cents")).alias(
                "period_change_cents"
            )
        )
        .sort("supplier_id", "period")
        .with_columns(
            pl.col("period_change_cents")
            .cum_sum()
            .over("supplier_id")
            .alias("balance_on_inventory_cents"),
            pl.lit("estimated").alias("status"),
        )
        .select(list(ROLLFORWARD_SCHEMA))
        .cast(ROLLFORWARD_SCHEMA)  # type: ignore[arg-type]
    )
    return RollforwardResult(
        table=table,
        notes=[
            "vendor rebate roll-forward is ESTIMATED: earned side uses receipt "
            "cost x program rate; recognized side uses the declared cost basis "
            "of goods sold, which can differ from receipt cost"
        ],
    )
