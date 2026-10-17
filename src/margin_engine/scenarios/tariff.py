"""Cost / tariff pass-through scenarios (Phase 7).

A scenario raises landed cost for a set of items (by item, supplier or
category) from an effective date. Pass-through strategies are shown side by
side (0% / 50% / 100% by default, plus segment-weighted); full pass-through is
never assumed. Contract-protected lines are excluded from pass-through and
their exposure reported as residual.

Outputs per scenario x strategy: incremental cost, recovered amount and %,
residual exposure, and expected contribution effect ONLY where the segment
elasticity is causal_elasticity_supported.

Overlap handling: a combined scenario (price action + cost shock) is modeled
jointly — the joint result is authoritative and overlapping single-scenario
numbers are never added together.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from margin_engine.config import Rules
from margin_engine.sensitivity.estimate import STATUS_CAUSAL


@dataclass
class CostShock:
    name: str
    scope: str  # "item" | "supplier" | "category"
    scope_value: str
    pct_increase: float  # e.g. 0.10 for +10% landed cost
    effective: date


def _scope_filter(shock: CostShock) -> pl.Expr:
    col = {"item": "item_id", "supplier": "supplier_id", "category": "item_category"}[shock.scope]
    return pl.col(col) == shock.scope_value


def pass_through_grid(
    panel: pl.DataFrame,
    guidance: pl.DataFrame,
    sensitivity: pl.DataFrame,
    shock: CostShock,
    rules: Rules,
    steps: int = 10,
) -> pl.DataFrame:
    """Precomputed pass-through grid for the hosted demo (Phase 18): the same
    baseline and math as run_cost_scenario, evaluated at pass-through levels
    0, 1/steps, ..., 1 so a static UI can slide between grid points without
    re-implementing engine logic. Contribution effect only where the segment
    elasticity is causal_elasticity_supported; contract-protected lines are
    excluded from pass-through and reported as exposure."""
    ts = rules.tariff_scenarios
    base = _scenario_baseline(panel, guidance, sensitivity, shock, rules)
    rows = []
    for k in range(steps + 1):
        level = k / steps
        work = base.with_columns(pl.lit(level).alias("_pt"))
        if ts.exclude_contract_protected:
            work = work.with_columns(
                pl.when(pl.col("protected")).then(0.0).otherwise(pl.col("_pt")).alias("_pt")
            )
        work = work.with_columns(
            (pl.col("cost_incr_pu") * pl.col("_pt") * pl.col("baseline_qty"))
            .round(0)
            .alias("recovered_cents"),
            (pl.col("price_cents") + pl.col("cost_incr_pu") * pl.col("_pt")).alias("p1"),
        )
        causal = pl.col("evidence_status") == STATUS_CAUSAL
        q1 = pl.col("baseline_qty") * (pl.col("p1") / pl.col("price_cents")).pow(
            pl.col("elasticity")
        )
        c0 = pl.col("unit_cost_cents")
        c1 = c0 + pl.col("cost_incr_pu")
        delta = (
            (pl.col("p1") - c1) * q1 - (pl.col("price_cents") - c0) * pl.col("baseline_qty")
        ).round(0)
        work = work.with_columns(
            pl.when(causal).then(delta).otherwise(None).alias("contribution_effect_cents")
        )
        rows.append(
            work.select(
                pl.lit(shock.name).alias("scenario"),
                pl.lit(level).alias("pass_through"),
                pl.col("cost_incr_cents").sum().round(0).alias("incremental_cost_cents"),
                pl.col("recovered_cents").sum().alias("recovered_cents"),
                pl.col("cost_incr_cents")
                .filter(pl.col("protected"))
                .sum()
                .round(0)
                .alias("protected_exposure_cents"),
                pl.col("contribution_effect_cents").sum().alias("contribution_effect_cents"),
                causal.all().alias("contribution_effect_supported_everywhere"),
                pl.len().alias("n_pairs"),
            )
        )
    return pl.concat(rows, how="vertical").with_columns(
        (pl.col("incremental_cost_cents") - pl.col("recovered_cents")).alias(
            "residual_exposure_cents"
        )
    )


def _scenario_baseline(
    panel: pl.DataFrame,
    guidance: pl.DataFrame,
    sensitivity: pl.DataFrame,
    shock: CostShock,
    rules: Rules,
) -> pl.DataFrame:
    """Shared baseline for scenario evaluation: pairs in scope over the
    baseline window with price, cost, elasticity (with evidence status),
    per-unit cost increase and protection flag. Sorted for determinism."""
    window = rules.opportunity.baseline_window_months
    max_m = int(panel["month_idx"].max() or 0)  # type: ignore[arg-type]

    base = (
        panel.filter(
            (pl.col("qty") > 0) & (pl.col("month_idx") > max_m - window) & _scope_filter(shock)
        )
        .sort("customer_id", "item_id", "month_idx")
        .group_by("customer_id", "item_id", "segment_leaf", "contract_id")
        .agg(
            pl.col("qty").sum().alias("baseline_qty"),
            (pl.col("price_cf") * pl.col("qty")).sum().alias("rev_cents"),
        )
        .sort("customer_id", "item_id")
        .with_columns((pl.col("rev_cents") / pl.col("baseline_qty")).alias("price_cents"))
    )
    cost = guidance.select("customer_id", "item_id", "unit_cost_cents")
    base = base.join(cost, on=["customer_id", "item_id"], how="left").filter(
        pl.col("unit_cost_cents").is_not_null()
    )
    sens = sensitivity.select(
        pl.col("segment").alias("segment_leaf"),
        pl.col("iv").alias("elasticity"),
        pl.col("status").alias("evidence_status"),
    )
    base = base.join(sens, on="segment_leaf", how="left").sort("customer_id", "item_id")
    return base.with_columns(
        (pl.col("unit_cost_cents") * shock.pct_increase).alias("cost_incr_pu"),
        pl.col("contract_id").is_not_null().alias("protected"),
    ).with_columns((pl.col("cost_incr_pu") * pl.col("baseline_qty")).alias("cost_incr_cents"))


def run_cost_scenario(
    panel: pl.DataFrame,
    guidance: pl.DataFrame,
    sensitivity: pl.DataFrame,
    shock: CostShock,
    rules: Rules,
    cost_method: str = "average",
) -> pl.DataFrame:
    """Evaluate one cost shock under each configured pass-through strategy."""
    ts = rules.tariff_scenarios
    base = _scenario_baseline(panel, guidance, sensitivity, shock, rules)

    rows = []
    strategies = [("none", 0.0), ("partial_50", 0.5), ("full", 1.0)]
    # Segment-weighted: pass through more where demand is less elastic.
    for name, pt_level in [*strategies, ("segment_weighted", None)]:
        work = base
        if pt_level is None:
            # weight = 1 / (1 + |elasticity|), normalized to average 0.5.
            work = work.with_columns(
                (1.0 / (1.0 + pl.col("elasticity").abs())).alias("_w")
            ).with_columns((0.5 * pl.col("_w") / pl.col("_w").mean()).clip(0.0, 1.0).alias("_pt"))
        else:
            work = work.with_columns(pl.lit(pt_level).alias("_pt"))
        if ts.exclude_contract_protected:
            work = work.with_columns(
                pl.when(pl.col("protected")).then(0.0).otherwise(pl.col("_pt")).alias("_pt")
            )
        work = work.with_columns(
            (pl.col("cost_incr_pu") * pl.col("_pt") * pl.col("baseline_qty"))
            .round(0)
            .alias("recovered_cents"),
            (pl.col("price_cents") + pl.col("cost_incr_pu") * pl.col("_pt")).alias("p1"),
        )
        causal = pl.col("evidence_status") == STATUS_CAUSAL
        q1 = pl.col("baseline_qty") * (pl.col("p1") / pl.col("price_cents")).pow(
            pl.col("elasticity")
        )
        c0 = pl.col("unit_cost_cents")
        c1 = c0 + pl.col("cost_incr_pu")
        delta = (
            (pl.col("p1") - c1) * q1 - (pl.col("price_cents") - c0) * pl.col("baseline_qty")
        ).round(0)
        work = work.with_columns(
            pl.when(causal).then(delta).otherwise(None).alias("contribution_effect_cents")
        )
        agg = work.select(
            pl.lit(shock.name).alias("scenario"),
            pl.lit(name).alias("strategy"),
            pl.col("cost_incr_cents").sum().round(0).alias("incremental_cost_cents"),
            pl.col("recovered_cents").sum().alias("recovered_cents"),
            pl.col("cost_incr_cents")
            .filter(pl.col("protected"))
            .sum()
            .round(0)
            .alias("protected_exposure_cents"),
            pl.col("contribution_effect_cents").sum().alias("contribution_effect_cents"),
            causal.all().alias("contribution_effect_supported_everywhere"),
            pl.len().alias("n_pairs"),
        ).with_columns(
            (pl.col("incremental_cost_cents") - pl.col("recovered_cents")).alias(
                "residual_exposure_cents"
            ),
            (pl.col("recovered_cents") / pl.col("incremental_cost_cents")).alias("recovered_pct"),
        )
        rows.append(agg)
    from margin_engine.extended.inventory import scenario_timing_statement

    return pl.concat(rows, how="vertical").with_columns(
        pl.lit(rules.tariff_scenarios.cost_pool).alias("cost_pool"),
        pl.lit(scenario_timing_statement(cost_method, rules.tariff_scenarios.cost_pool)).alias(
            "cogs_timing"
        ),
    )


def combined_scenario_no_double_count(
    panel: pl.DataFrame,
    guidance: pl.DataFrame,
    sensitivity: pl.DataFrame,
    shocks: list[CostShock],
    rules: Rules,
) -> pl.DataFrame:
    """Joint evaluation of multiple shocks: each pair's cost increase is the
    compounded effect of every shock whose scope covers it; no pair is counted
    twice even when scopes overlap (e.g. a supplier shock and a category shock
    hitting the same items)."""
    window = rules.opportunity.baseline_window_months
    max_m = int(panel["month_idx"].max() or 0)  # type: ignore[arg-type]
    base = (
        panel.filter((pl.col("qty") > 0) & (pl.col("month_idx") > max_m - window))
        .group_by("customer_id", "item_id", "supplier_id", "item_category", "contract_id")
        .agg(pl.col("qty").sum().alias("baseline_qty"))
    )
    cost = guidance.select("customer_id", "item_id", "unit_cost_cents")
    base = base.join(cost, on=["customer_id", "item_id"], how="left").filter(
        pl.col("unit_cost_cents").is_not_null()
    )
    factor = pl.lit(1.0)
    hit = pl.lit(False)
    for s in shocks:
        in_scope = _scope_filter(s)
        factor = factor * pl.when(in_scope).then(1.0 + s.pct_increase).otherwise(1.0)
        hit = hit | in_scope
    base = base.filter(hit).with_columns(
        ((factor - 1.0) * pl.col("unit_cost_cents") * pl.col("baseline_qty"))
        .round(0)
        .alias("cost_incr_cents")
    )
    return base.select(
        pl.lit("+".join(s.name for s in shocks)).alias("scenario"),
        pl.col("cost_incr_cents").sum().alias("incremental_cost_cents"),
        pl.len().alias("n_pairs"),
    )
