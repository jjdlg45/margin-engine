"""Action generation (Phase 38): engine findings become proposed actions.

Every action is generated deterministically from engine outputs, carries a
plain-English title and rationale whose every number comes from those outputs,
an evidence status, and an expected dollar impact at the RIGHT opportunity
level for its type — levels are never summed across types:

- price_change        level 3 (expected contribution change) when the volume
                      response is causal-supported and inside price support;
                      otherwise level 2 (gross price opportunity) at fixed
                      volume, labeled.
- below_floor_fix     raise to the binding floor at fixed volume (pocket
                      dollars; conservative by construction).
- cost_pass_through   recovered cost at the configured pass-through level
                      (never assumed full), from the scenario grid.
- service_policy      the contribution at stake after activity costs
                      (estimated: activity rates are config estimates).
- rebate_discount_review  level-1 observed leakage in excess of the customer's
                      segment-median leakage rate.

Guardrails enforced here:
- no action proposes a price below the binding floor (defensive error — the
  guidance target already respects it);
- expected volume response only where evidence is causal-supported AND inside
  identifying price support (sizing's volume_treatment); otherwise the impact
  is fixed-volume and labeled;
- actions on contract-protected lines are generated BLOCKED with the reason
  shown (they can never be approved);
- a Robinson-Patman review flag travels with the action (approval will demand
  a written justification, Phase 39).

Dates are anchored to the data's last invoice date, so the same world always
generates byte-identical actions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

import polars as pl

from margin_engine import __version__
from margin_engine.actions.model import empty_events
from margin_engine.config import Rules
from margin_engine.scenarios.tariff import CostShock, pass_through_grid

ACTIONS_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "action_id": pl.String,
    "action_type": pl.String,
    "title": pl.String,
    "rationale": pl.String,
    "customer_id": pl.String,
    "item_id": pl.String,
    "scope": pl.String,
    "uom": pl.String,
    "segment_leaf": pl.Int64,
    "guidance_status": pl.String,
    "evidence_status": pl.String,
    "volume_treatment": pl.String,
    "predicted_volume_ratio": pl.Float64,
    "impact_basis": pl.String,
    "expected_impact_cents": pl.Int64,
    "current_price_cents": pl.Int64,
    "proposed_price_cents": pl.Int64,
    "floor_price_cents": pl.Int64,
    "priority": pl.String,
    "owner": pl.String,
    "due_date": pl.Date,
    "compliance_flags": pl.String,
    "blocked": pl.Boolean,
    "blocked_reason": pl.String,
    "created_at": pl.Datetime("us", "UTC"),
}


@dataclass
class ActionGenerationResult:
    actions: pl.DataFrame
    events: pl.DataFrame
    notes: list[str] = field(default_factory=list)


class FloorGuardrailError(RuntimeError):
    """A generated action proposed a price below the binding floor — a logic
    error upstream, never something to clamp silently."""


def _usd(cents: int | float | None) -> str:
    if cents is None:
        return "-"
    return f"${cents / 100:,.2f}"


def _usd_expr(col: str) -> pl.Expr:
    """'$1,234.06'-style formatting is not needed inside expressions; this
    yields exact '$12.34' strings (sign, dollars, two-digit cents)."""
    cents = pl.col(col).cast(pl.Float64).round(0).cast(pl.Int64)
    sign = pl.when(cents < 0).then(pl.lit("-$")).otherwise(pl.lit("$"))
    return pl.concat_str(
        [
            sign,
            (cents.abs() // 100).cast(pl.String),
            pl.lit("."),
            (cents.abs() % 100).cast(pl.String).str.zfill(2),
        ]
    )


def _action_id(*parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return f"act_{h[:12]}"


def _priority_expr(rules: Rules) -> pl.Expr:
    pc = rules.actions.priority
    impact = pl.col("expected_impact_cents").fill_null(0).abs()
    base = (
        pl.when(impact >= pc.high_impact_cents)
        .then(pl.lit("high"))
        .when(impact >= pc.medium_impact_cents)
        .then(pl.lit("medium"))
        .otherwise(pl.lit("low"))
    )
    if not pc.demote_without_causal:
        return base
    weak = pl.col("volume_treatment").fill_null("") != "expected_change"
    return (
        pl.when(weak & (base == pl.lit("high")))
        .then(pl.lit("medium"))
        .when(weak & (base == pl.lit("medium")))
        .then(pl.lit("low"))
        .otherwise(base)
    )


def _finalize(
    rows: pl.DataFrame, rules: Rules, anchor: dt.date, created: dt.datetime
) -> pl.DataFrame:
    """Fill priority, owner, due date and created_at; cap per type by impact;
    stable output order."""
    ac = rules.actions
    due_days = {"high": ac.due_days_high, "medium": ac.due_days_medium, "low": ac.due_days_low}
    out = rows.with_columns(_priority_expr(rules).alias("priority"))
    out = out.with_columns(
        pl.lit(ac.default_owner).alias("owner"),
        (
            pl.lit(anchor)
            + pl.duration(days=pl.col("priority").replace_strict(due_days, return_dtype=pl.Int64))
        ).alias("due_date"),
        pl.lit(created).alias("created_at"),
    )
    out = (
        out.sort(
            ["expected_impact_cents", "action_id"],
            descending=[True, False],
            nulls_last=True,
        )
        .group_by("action_type", maintain_order=True)
        .head(ac.max_actions_per_type)
    )
    return out.sort(["action_type", "action_id"]).select(list(ACTIONS_SCHEMA))


def _pair_base(
    guidance: pl.DataFrame,
    opportunities: pl.DataFrame,
    ledger: pl.DataFrame,
    sensitivity: pl.DataFrame,
) -> pl.DataFrame:
    """Guidance pairs with their opportunity row (prices, baseline volume,
    impact levels) joined on, plus the pair's normalized selling UOM (for the
    ERP price export, Phase 40). Floor price in cents from the floor index and
    the pair's effective list (the engine compares in index space; 24.4)."""
    if "uom_norm" in ledger.columns:
        pair_uom = (
            ledger.filter(pl.col("transaction_type") == "sale")
            .group_by("customer_id", "item_id")
            .agg(pl.col("uom_norm").drop_nulls().cast(pl.String).mode().sort().first().alias("uom"))
        )
    else:
        pair_uom = None
    opp_cols = [
        "customer_id",
        "item_id",
        "baseline_qty",
        "current_invoice_price_cents",
        "proposed_invoice_price_cents",
        "gross_price_opportunity_cents",
        "expected_contribution_change_cents",
        "volume_treatment",
        "evidence_status",
        "pocket_retention",
    ]
    opp = opportunities.select([c for c in opp_cols if c in opportunities.columns])
    g = guidance.join(opp, on=["customer_id", "item_id"], how="left", maintain_order="left")
    if pair_uom is not None:
        g = g.join(pair_uom, on=["customer_id", "item_id"], how="left", maintain_order="left")
    else:
        g = g.with_columns(pl.lit(None, dtype=pl.String).alias("uom"))
    if {"segment", "iv"}.issubset(sensitivity.columns):
        g = g.join(
            sensitivity.select(
                pl.col("segment").alias("segment_leaf"),
                pl.col("iv").cast(pl.Float64).alias("_seg_elasticity"),
            ),
            on="segment_leaf",
            how="left",
            maintain_order="left",
        )
    else:
        g = g.with_columns(pl.lit(None, dtype=pl.Float64).alias("_seg_elasticity"))
    return g.with_columns(
        pl.when(pl.col("floor_index").is_not_null() & (pl.col("eff_list_cents") > 0))
        .then((pl.col("floor_index") * pl.col("eff_list_cents")).round(0).cast(pl.Int64))
        .otherwise(None)
        .alias("_floor_price_cents"),
        pl.when(pl.col("rp_review_flag").fill_null(value=False))
        .then(pl.lit("robinson_patman"))
        .otherwise(pl.lit(""))
        .alias("_compliance")
        if "rp_review_flag" in g.columns
        else pl.lit("").alias("_compliance"),
    )


def _below_floor_actions(base: pl.DataFrame, anchor: dt.date) -> pl.DataFrame:
    rows = base.filter(pl.col("guidance_status") == "below_floor")
    if rows.height == 0:
        return pl.DataFrame()
    rows = rows.with_columns(
        pl.col("current_invoice_price_cents").round(0).cast(pl.Int64).alias("_cur"),
        pl.col("_floor_price_cents").alias("_prop"),
        # Fixed-volume pocket impact of raising to the floor; null when the
        # pair had no baseline volume in the window (stated, never invented).
        pl.when(
            pl.col("baseline_qty").is_not_null()
            & pl.col("_floor_price_cents").is_not_null()
            & pl.col("current_invoice_price_cents").is_not_null()
        )
        .then(
            (
                (pl.col("_floor_price_cents") - pl.col("current_invoice_price_cents"))
                * pl.col("baseline_qty")
                * pl.col("pocket_retention").fill_null(1.0)
            )
            .round(0)
            .cast(pl.Int64)
        )
        .otherwise(None)
        .alias("expected_impact_cents"),
    )
    return rows.select(
        pl.concat_str(
            [
                pl.lit("below_floor_fix"),
                pl.col("customer_id"),
                pl.col("item_id"),
                pl.lit(str(anchor)),
            ],
            separator="|",
        )
        .map_elements(lambda s: _action_id(s), return_dtype=pl.String)
        .alias("action_id"),
        pl.lit("below_floor_fix").alias("action_type"),
        pl.concat_str(
            [
                pl.lit("Fix below-floor price: customer "),
                pl.col("customer_id"),
                pl.lit(", item "),
                pl.col("item_id"),
            ]
        ).alias("title"),
        pl.concat_str(
            [
                pl.lit("This pair is priced below the binding "),
                pl.col("binding_floor_basis")
                .fill_null("margin")
                .replace(
                    {
                        "pocket_gross_margin": "pocket gross margin",
                        "contribution_margin": "contribution margin",
                        "contribution": "contribution margin",
                    }
                ),
                pl.lit(" floor of "),
                _usd_expr("_floor_price_cents"),
                pl.lit(" per unit (invoice basis). Raising to the floor at unchanged "),
                pl.lit("volume is the minimum fix; the impact shown is that fixed-volume "),
                pl.lit("pocket amount over the baseline window."),
            ]
        ).alias("rationale"),
        pl.col("customer_id"),
        pl.col("item_id"),
        pl.lit("customer_item").alias("scope"),
        pl.col("uom"),
        pl.col("segment_leaf").cast(pl.Int64),
        pl.col("guidance_status"),
        pl.col("evidence_status"),
        pl.lit("fixed_volume_only").alias("volume_treatment"),
        pl.lit(1.0).alias("predicted_volume_ratio"),
        pl.when(pl.col("expected_impact_cents").is_not_null())
        .then(pl.lit("floor_gap_fixed_volume_pocket"))
        .otherwise(pl.lit("no_recent_volume"))
        .alias("impact_basis"),
        pl.col("expected_impact_cents"),
        pl.col("_cur").alias("current_price_cents"),
        pl.col("_prop").alias("proposed_price_cents"),
        pl.col("_floor_price_cents").alias("floor_price_cents"),
        pl.col("_compliance").alias("compliance_flags"),
        pl.col("contract_protected").fill_null(value=False).alias("blocked"),
        pl.when(pl.col("contract_protected").fill_null(value=False))
        .then(
            pl.lit(
                "contract-protected line: price actions are blocked until the "
                "price protection lapses"
            )
        )
        .otherwise(None)
        .alias("blocked_reason"),
    )


def _price_change_actions(base: pl.DataFrame, anchor: dt.date) -> pl.DataFrame:
    rows = base.filter(
        (pl.col("guidance_status") != "below_floor")
        & pl.col("gross_price_opportunity_cents").is_not_null()
        & (pl.col("gross_price_opportunity_cents") > 0)
        & ~pl.col("contract_protected").fill_null(value=False)
    )
    if rows.height == 0:
        return pl.DataFrame()
    expected = pl.col("volume_treatment") == "expected_change"
    rows = rows.with_columns(
        pl.when(expected)
        .then(pl.col("expected_contribution_change_cents"))
        .otherwise(pl.col("gross_price_opportunity_cents"))
        .round(0)
        .cast(pl.Int64)
        .alias("expected_impact_cents"),
        pl.when(expected)
        .then(pl.lit("expected_contribution_change"))
        .otherwise(pl.lit("gross_price_opportunity_fixed_volume"))
        .alias("impact_basis"),
        pl.col("current_invoice_price_cents").round(0).cast(pl.Int64).alias("_cur"),
        pl.col("proposed_invoice_price_cents").round(0).cast(pl.Int64).alias("_prop"),
    )
    return rows.select(
        pl.concat_str(
            [
                pl.lit("price_change"),
                pl.col("customer_id"),
                pl.col("item_id"),
                pl.lit(str(anchor)),
            ],
            separator="|",
        )
        .map_elements(lambda s: _action_id(s), return_dtype=pl.String)
        .alias("action_id"),
        pl.lit("price_change").alias("action_type"),
        pl.concat_str(
            [
                pl.lit("Raise price: customer "),
                pl.col("customer_id"),
                pl.lit(", item "),
                pl.col("item_id"),
            ]
        ).alias("title"),
        pl.concat_str(
            [
                pl.lit("Current price sits below the segment reference target "),
                pl.lit("(policy target, never below the floor). "),
                pl.when(expected)
                .then(
                    pl.lit(
                        "The volume response is causal-supported and inside the "
                        "identified price range, so the impact is the expected "
                        "contribution change."
                    )
                )
                .otherwise(
                    pl.lit(
                        "The volume evidence does not support predicting a response, "
                        "so the impact is the gross price opportunity at UNCHANGED "
                        "volume (labeled fixed-volume)."
                    )
                ),
            ]
        ).alias("rationale"),
        pl.col("customer_id"),
        pl.col("item_id"),
        pl.lit("customer_item").alias("scope"),
        pl.col("uom"),
        pl.col("segment_leaf").cast(pl.Int64),
        pl.col("guidance_status"),
        pl.col("evidence_status"),
        pl.col("volume_treatment"),
        pl.when(
            expected
            & pl.col("_seg_elasticity").is_not_null()
            & (pl.col("_cur") > 0)
            & pl.col("_prop").is_not_null()
        )
        .then((pl.col("_prop") / pl.col("_cur")).pow(pl.col("_seg_elasticity")))
        .otherwise(1.0)
        .round(6)
        .alias("predicted_volume_ratio"),
        pl.col("impact_basis"),
        pl.col("expected_impact_cents"),
        pl.col("_cur").alias("current_price_cents"),
        pl.col("_prop").alias("proposed_price_cents"),
        pl.col("_floor_price_cents").alias("floor_price_cents"),
        pl.col("_compliance").alias("compliance_flags"),
        pl.lit(value=False).alias("blocked"),
        pl.lit(None, dtype=pl.String).alias("blocked_reason"),
    )


def _service_policy_actions(service_flags: pl.DataFrame | None, anchor: dt.date) -> pl.DataFrame:
    if service_flags is None or service_flags.height == 0:
        return pl.DataFrame()
    rows = service_flags.with_columns(
        (-pl.col("contribution_after_activity_cents"))
        .clip(lower_bound=0)
        .alias("expected_impact_cents"),
        pl.when(pl.col("flag_minimum_order_policy"))
        .then(pl.lit("minimum order value"))
        .otherwise(pl.lit("delivery frequency"))
        .alias("_policy"),
    )
    return rows.select(
        pl.concat_str(
            [pl.lit("service_policy"), pl.col("customer_id"), pl.lit(str(anchor))], separator="|"
        )
        .map_elements(lambda s: _action_id(s), return_dtype=pl.String)
        .alias("action_id"),
        pl.lit("service_policy").alias("action_type"),
        pl.concat_str(
            [pl.lit("Review "), pl.col("_policy"), pl.lit(" for customer "), pl.col("customer_id")]
        ).alias("title"),
        pl.concat_str(
            [
                pl.lit("This customer loses money after activity costs: contribution "),
                pl.lit("after activity is "),
                _usd_expr("contribution_after_activity_cents"),
                pl.lit(" over the period, on an average order of "),
                _usd_expr("avg_order_value_cents"),
                pl.lit(". A service-policy change (not a price action): the amount "),
                pl.lit("shown is the contribution at stake, estimated from configured "),
                pl.lit("activity rates."),
            ]
        ).alias("rationale"),
        pl.col("customer_id"),
        pl.lit(None, dtype=pl.String).alias("item_id"),
        pl.lit("customer").alias("scope"),
        pl.lit(None, dtype=pl.String).alias("uom"),
        pl.lit(None, dtype=pl.Int64).alias("segment_leaf"),
        pl.lit(None, dtype=pl.String).alias("guidance_status"),
        pl.lit("estimated").alias("evidence_status"),
        pl.lit("fixed_volume_only").alias("volume_treatment"),
        pl.lit(None, dtype=pl.Float64).alias("predicted_volume_ratio"),
        pl.lit("contribution_at_stake_estimated").alias("impact_basis"),
        pl.col("expected_impact_cents").cast(pl.Int64),
        pl.lit(None, dtype=pl.Int64).alias("current_price_cents"),
        pl.lit(None, dtype=pl.Int64).alias("proposed_price_cents"),
        pl.lit(None, dtype=pl.Int64).alias("floor_price_cents"),
        pl.lit("").alias("compliance_flags"),
        pl.lit(value=False).alias("blocked"),
        pl.lit(None, dtype=pl.String).alias("blocked_reason"),
    )


def _rebate_review_actions(
    leakage: pl.DataFrame, guidance: pl.DataFrame, rules: Rules, anchor: dt.date
) -> pl.DataFrame:
    ac = rules.actions
    if leakage.height == 0 or guidance.height == 0:
        return pl.DataFrame()
    cust_seg = (
        guidance.drop_nulls("segment_leaf")
        .group_by("customer_id")
        .agg(pl.col("segment_leaf").mode().sort().first().alias("segment_leaf"))
    )
    lk = (
        leakage.filter(pl.col("net_revenue_cents") > 0)
        .join(cust_seg, on="customer_id", how="inner")
        .with_columns((pl.col("total_leakage_cents") / pl.col("net_revenue_cents")).alias("_rate"))
        .with_columns(pl.col("_rate").median().over("segment_leaf").alias("_seg_median"))
        .with_columns(
            ((pl.col("_rate") - pl.col("_seg_median")) * pl.col("net_revenue_cents"))
            .round(0)
            .cast(pl.Int64)
            .alias("expected_impact_cents")
        )
        .filter(
            (pl.col("_seg_median") > 0)
            & (pl.col("_rate") > pl.col("_seg_median") * ac.leakage_review_min_ratio)
            & (pl.col("expected_impact_cents") >= ac.leakage_review_min_excess_cents)
        )
    )
    if lk.height == 0:
        return pl.DataFrame()
    return lk.select(
        pl.concat_str(
            [pl.lit("rebate_discount_review"), pl.col("customer_id"), pl.lit(str(anchor))],
            separator="|",
        )
        .map_elements(lambda s: _action_id(s), return_dtype=pl.String)
        .alias("action_id"),
        pl.lit("rebate_discount_review").alias("action_type"),
        pl.concat_str(
            [pl.lit("Review rebates and discounts for customer "), pl.col("customer_id")]
        ).alias("title"),
        pl.concat_str(
            [
                pl.lit("Leakage (discounts, rebates, concessions, price credits) is "),
                (pl.col("_rate") * 100).round(1).cast(pl.String),
                pl.lit("% of this customer's revenue vs a segment median of "),
                (pl.col("_seg_median") * 100).round(1).cast(pl.String),
                pl.lit("%. The amount shown is the observed level-1 leakage in excess "),
                pl.lit("of the segment-median rate - reconciled dollars, not a forecast; "),
                pl.lit("it is never summed with price-opportunity levels."),
            ]
        ).alias("rationale"),
        pl.col("customer_id"),
        pl.lit(None, dtype=pl.String).alias("item_id"),
        pl.lit("customer").alias("scope"),
        pl.lit(None, dtype=pl.String).alias("uom"),
        pl.col("segment_leaf").cast(pl.Int64),
        pl.lit(None, dtype=pl.String).alias("guidance_status"),
        pl.lit("actual").alias("evidence_status"),
        pl.lit(None, dtype=pl.String).alias("volume_treatment"),
        pl.lit(None, dtype=pl.Float64).alias("predicted_volume_ratio"),
        pl.lit("observed_leakage_excess").alias("impact_basis"),
        pl.col("expected_impact_cents"),
        pl.lit(None, dtype=pl.Int64).alias("current_price_cents"),
        pl.lit(None, dtype=pl.Int64).alias("proposed_price_cents"),
        pl.lit(None, dtype=pl.Int64).alias("floor_price_cents"),
        pl.lit("").alias("compliance_flags"),
        pl.lit(value=False).alias("blocked"),
        pl.lit(None, dtype=pl.String).alias("blocked_reason"),
    )


def _pass_through_actions(
    ledger: pl.DataFrame,
    panel: pl.DataFrame | None,
    guidance: pl.DataFrame,
    sensitivity: pl.DataFrame,
    rules: Rules,
    anchor: dt.date,
    notes: list[str],
) -> pl.DataFrame:
    ac = rules.actions
    if ac.max_pass_through_scenarios == 0 or panel is None or panel.height == 0:
        return pl.DataFrame()
    sale = ledger.filter(pl.col("transaction_type") == "sale").select("item_id", "net_amount_cents")
    rows: list[dict[str, object]] = []
    level = ac.pass_through_level
    for scope, col in (("category", "item_category"), ("supplier", "supplier_id")):
        if len(rows) >= ac.max_pass_through_scenarios:
            break
        if col not in panel.columns:
            continue
        top = (
            sale.join(
                panel.select("item_id", col).unique(subset=["item_id"]), on="item_id", how="left"
            )
            .filter(pl.col(col).is_not_null())
            .group_by(col)
            .agg(pl.col("net_amount_cents").sum().alias("_rev"))
            .sort(["_rev", col], descending=[True, False])
            .head(1)[col]
            .to_list()
        )
        if not top:
            continue
        value = str(top[0])
        shock = CostShock(
            name=f"+10% landed cost: {scope} {value}",
            scope=scope,
            scope_value=value,
            pct_increase=0.10,
            effective=dt.date(2000, 1, 1),
        )
        grid = pass_through_grid(panel, guidance, sensitivity, shock, rules, steps=10)
        if grid.height == 0 or int(grid.select(pl.col("n_pairs").max()).item() or 0) == 0:
            continue
        point = (
            grid.with_columns((pl.col("pass_through") - level).abs().alias("_d"))
            .sort("_d")
            .head(1)
            .to_dicts()[0]
        )
        incr = int(point["incremental_cost_cents"] or 0)
        rec = int(point["recovered_cents"] or 0)
        resid = int(point["residual_exposure_cents"] or 0)
        prot = int(point["protected_exposure_cents"] or 0)
        rows.append(
            {
                "action_id": _action_id("cost_pass_through", scope, value, str(anchor)),
                "action_type": "cost_pass_through",
                "title": (
                    f"Pass through {level:.0%} of a +10% landed-cost increase on {scope} {value}"
                ),
                "rationale": (
                    f"A +10% landed-cost increase on this {scope} adds "
                    f"{_usd(incr)} of cost over the baseline window. Passing "
                    f"through {level:.0%} recovers {_usd(rec)} at unchanged "
                    f"volume, leaving {_usd(resid)} unrecovered"
                    + (
                        f" (of which {_usd(prot)} sits on contract-protected "
                        f"lines that cannot take an increase)"
                        if prot
                        else ""
                    )
                    + ". Full pass-through is never assumed."
                ),
                "customer_id": None,
                "item_id": None,
                "scope": f"{scope}:{value}",
                "uom": None,
                "segment_leaf": None,
                "guidance_status": None,
                "evidence_status": "actual",
                "volume_treatment": "fixed_volume_only",
                "predicted_volume_ratio": None,
                "impact_basis": "cost_recovery_at_chosen_level",
                "expected_impact_cents": rec,
                "current_price_cents": None,
                "proposed_price_cents": None,
                "floor_price_cents": None,
                "compliance_flags": "",
                "blocked": False,
                "blocked_reason": None,
            }
        )
    if rows:
        notes.append(
            f"cost pass-through actions use the configured level "
            f"({level:.0%}); contract-protected lines are excluded by the "
            f"scenario engine"
        )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def generate_actions(
    *,
    ledger: pl.DataFrame,
    guidance: pl.DataFrame,
    opportunities: pl.DataFrame,
    leakage: pl.DataFrame,
    service_flags: pl.DataFrame | None,
    sensitivity: pl.DataFrame,
    panel: pl.DataFrame | None,
    rules: Rules,
) -> ActionGenerationResult:
    notes: list[str] = []
    if not rules.actions.enabled:
        return ActionGenerationResult(
            actions=pl.DataFrame(schema=ACTIONS_SCHEMA),
            events=empty_events(),
            notes=["action generation OFF: disabled in config"],
        )
    anchor_raw = ledger["invoice_date"].max()
    anchor: dt.date = anchor_raw if isinstance(anchor_raw, dt.date) else dt.date(1970, 1, 1)
    created = dt.datetime(anchor.year, anchor.month, anchor.day, tzinfo=dt.UTC)

    base = _pair_base(guidance, opportunities, ledger, sensitivity)
    parts = [
        _below_floor_actions(base, anchor),
        _price_change_actions(base, anchor),
        _pass_through_actions(ledger, panel, guidance, sensitivity, rules, anchor, notes),
        _service_policy_actions(service_flags, anchor),
        _rebate_review_actions(leakage, guidance, rules, anchor),
    ]
    frames = [p for p in parts if p.height > 0]
    if not frames:
        return ActionGenerationResult(
            actions=pl.DataFrame(schema=ACTIONS_SCHEMA), events=empty_events(), notes=notes
        )
    combined = pl.concat(
        [
            f.select(
                [
                    c
                    for c in ACTIONS_SCHEMA
                    if c not in ("priority", "owner", "due_date", "created_at")
                ]
            )
            for f in frames
        ],
        how="vertical_relaxed",
    )
    actions = _finalize(combined, rules, anchor, created)

    # Guardrail: no unblocked action may propose a price below the binding
    # floor. The guidance target respects the floor by construction, so a hit
    # here is a logic error to surface, never to clamp away.
    bad = actions.filter(
        ~pl.col("blocked")
        & pl.col("proposed_price_cents").is_not_null()
        & pl.col("floor_price_cents").is_not_null()
        & (pl.col("proposed_price_cents") < pl.col("floor_price_cents"))
    )
    if bad.height:
        ids = bad["action_id"].head(5).to_list()
        msg = (
            f"{bad.height} generated action(s) propose a price below the binding floor, e.g. {ids}"
        )
        raise FloorGuardrailError(msg)

    events = empty_events()
    note = f"generated by margin-engine {__version__}"
    birth = actions.select(
        pl.col("action_id"),
        pl.lit(0, dtype=pl.Int64).alias("seq"),
        pl.lit("engine").alias("who"),
        pl.col("created_at").alias("at"),
        pl.lit(None, dtype=pl.String).alias("from_status"),
        pl.lit("proposed").alias("to_status"),
        pl.lit(note).alias("note"),
    )
    events = pl.concat([events, birth], how="vertical")
    notes.append(
        f"{actions.height} action(s) generated across "
        f"{actions['action_type'].n_unique()} type(s); impact levels are "
        "per-type and never summed across types"
    )
    return ActionGenerationResult(actions=actions, events=events, notes=notes)
