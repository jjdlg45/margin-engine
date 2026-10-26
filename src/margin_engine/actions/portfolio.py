"""Portfolio scenarios for overlapping action scopes (Run 6, Phase 68).

A category pass-through and a pair price change on the same items answer the
same question twice: summing their single-action impacts would double count.
Actions that share a scope carry a common `overlap_group`, and this module
computes each group's JOINT estimate under one baseline:

- price actions keep their single-action impacts, de-duplicated by scope_key
  (one active proposed price per customer x item x UOM x effective date);
- the pass-through recovery is RECOMPUTED with the pairs that have their own
  price action excluded from its eligible base.

The portfolio estimate is NON-ADDITIVE with the single-action lists: it is a
separate figure with its own label, shown beside the naive sum precisely so a
reader can see what double counting would have claimed.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from margin_engine.config import Rules
from margin_engine.scenarios.tariff import CostShock, pass_through_grid

PORTFOLIO_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "overlap_group": pl.String,
    "n_actions": pl.Int64,
    "action_ids": pl.String,  # ';'-joined, deterministic order
    "naive_sum_cents": pl.Int64,
    "portfolio_estimate_cents": pl.Int64,
    "note": pl.String,
}

_PRICE_TYPES = ("price_change", "below_floor_fix")


def build_portfolio_groups(
    actions: pl.DataFrame,
    panel: pl.DataFrame | None,
    guidance: pl.DataFrame,
    sensitivity: pl.DataFrame,
    rules: Rules,
) -> pl.DataFrame:
    """One row per overlap_group: the naive sum (what double counting would
    claim) and the joint portfolio estimate under a common baseline."""
    if actions.height == 0 or "overlap_group" not in actions.columns:
        return pl.DataFrame(schema=PORTFOLIO_SCHEMA)
    groups = sorted(g for g in actions["overlap_group"].unique().to_list() if g is not None)
    rows: list[dict[str, object]] = []
    for group in groups:
        in_group = actions.filter(
            (pl.col("overlap_group") == group) & ~pl.col("blocked").fill_null(value=False)
        ).sort("action_id")
        if in_group.height == 0:
            continue
        naive = int(in_group["expected_impact_cents"].fill_null(0).sum())
        price_rows = in_group.filter(pl.col("action_type").is_in(list(_PRICE_TYPES)))
        # One active proposed price per scope key: deterministic first wins.
        price_rows = price_rows.sort("scope_key", "action_id").unique(
            subset=["scope_key"], keep="first", maintain_order=True
        )
        price_part = int(price_rows["expected_impact_cents"].fill_null(0).sum())
        covered_pairs = price_rows.select(
            pl.col("customer_id").cast(pl.String), pl.col("item_id").cast(pl.String)
        ).unique()

        pt_part = 0
        pt_rows = in_group.filter(pl.col("action_type") == "cost_pass_through")
        for r in pt_rows.sort("action_id").iter_rows(named=True):
            scope = str(r["scope"])
            kind, _, value = scope.partition(":")
            level = float(r["pass_through_level"] or 0.0)
            if panel is None or panel.height == 0:
                continue
            residual_panel = panel.with_columns(
                pl.col("customer_id").cast(pl.String).alias("_c"),
                pl.col("item_id").cast(pl.String).alias("_i"),
            ).join(
                covered_pairs.rename({"customer_id": "_c", "item_id": "_i"}),
                on=["_c", "_i"],
                how="anti",
            )
            shock = CostShock(
                name=f"portfolio {scope}",
                scope=kind,
                scope_value=value,
                pct_increase=0.10,
                effective=dt.date(2000, 1, 1),
            )
            grid = pass_through_grid(
                residual_panel.drop("_c", "_i"), guidance, sensitivity, shock, rules, steps=10
            )
            if grid.height == 0:
                continue
            point = (
                grid.with_columns((pl.col("pass_through") - level).abs().alias("_d"))
                .sort("_d")
                .head(1)
                .to_dicts()[0]
            )
            pt_part += int(point["recovered_cents"] or 0)
        portfolio = price_part + pt_part
        rows.append(
            {
                "overlap_group": group,
                "n_actions": in_group.height,
                "action_ids": ";".join(in_group["action_id"].to_list()),
                "naive_sum_cents": naive,
                "portfolio_estimate_cents": portfolio,
                "note": (
                    "joint estimate under one baseline: pairs with their own price "
                    "action are excluded from the pass-through recovery; the naive "
                    "sum is shown only to make the double counting visible"
                ),
            }
        )
    return (
        pl.DataFrame(rows, schema=PORTFOLIO_SCHEMA).sort("overlap_group")
        if rows
        else pl.DataFrame(schema=PORTFOLIO_SCHEMA)
    )
