"""Generator orchestration: one seed in, a full fake ERP world out.

Usage:
    uv run python -m synth.generate --seed 42 --preset well_identified --out data/synth
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import typer

from synth.config import GeneratorConfig, Preset
from synth.demand import build_pairs, simulate_panel
from synth.entities import (
    build_contracts,
    build_customers,
    build_items,
    build_list_price_history,
    build_rebate_programs,
    build_segments,
    build_substitution_groups,
    build_uom_conversions,
)
from synth.lines import add_deductions, build_lines
from synth.mess import (
    add_credit_memos,
    add_duplicates,
    add_free_goods,
    add_kits,
    add_returns,
    add_zero_qty_rows,
    blank_costs,
    corrupt_prices,
    reexpress_uom,
)
from synth.output import (
    build_fx_rates,
    convert_lines_to_local,
    export_erp_lines,
    export_ground_truth,
    export_reference_tables,
)
from synth.quotes import plant_quotes
from synth.tiered import plant_tiered_rebates

_DEFAULT_COLUMNS: dict[str, Any] = {
    "kit_id": None,
    "truth_small_order_customer": False,
    "credit_reason": None,
    "return_reason_code": None,
    "original_invoice_ref": None,
    "truth_zero_qty": False,
    "truth_uom_alt": False,
    "truth_dup_type": "",
    "truth_outlier_type": "",
}
_DEFAULT_INT_COLUMNS = ["restock_fee_cents", "rtn_frt_billed_cents", "truth_rtn_frt_exp_cents"]


def _ensure_columns(lines: pl.DataFrame) -> pl.DataFrame:
    missing = [
        pl.lit(v, dtype=pl.String if v is None or isinstance(v, str) else None).alias(k)
        for k, v in _DEFAULT_COLUMNS.items()
        if k not in lines.columns
    ]
    lines = lines.with_columns(missing) if missing else lines
    int_missing = [
        pl.lit(0, dtype=pl.Int64).alias(c) for c in _DEFAULT_INT_COLUMNS if c not in lines.columns
    ]
    return lines.with_columns(int_missing) if int_missing else lines


def generate(cfg: GeneratorConfig, out_dir: Path) -> dict[str, Any]:
    """Run the full generator; returns the ground-truth summary dict."""
    rng = np.random.default_rng(cfg.seed)
    erp_dir = out_dir / "erp"
    erp_dir.mkdir(parents=True, exist_ok=True)

    segments = build_segments(cfg, rng)
    customers = build_customers(cfg, rng)
    items = build_items(cfg, rng)
    list_history = build_list_price_history(cfg, items, rng)
    contracts = build_contracts(cfg, customers, items, rng)
    rebate_programs = build_rebate_programs(cfg, customers, rng)
    uom_conversions = build_uom_conversions(items, rng)

    sub_groups, sub_gammas = build_substitution_groups(cfg, items)
    partner: dict[str, str] = {}
    if sub_groups.height:
        for (_,), grp in sub_groups.group_by(["group_id"]):
            ids = grp["item_id"].to_list()
            partner[ids[0]], partner[ids[1]] = ids[1], ids[0]
    pairs = build_pairs(
        cfg,
        customers,
        items,
        contracts,
        segments,
        rng,
        substitution_partner=partner or None,
    )
    world = simulate_panel(
        cfg,
        pairs,
        items,
        customers,
        list_history,
        rng,
        substitution=(sub_groups, sub_gammas) if sub_groups.height else None,
    )

    lines = build_lines(cfg, world, customers, items, rng)
    lines = corrupt_prices(cfg, lines, rng)
    lines = add_deductions(cfg, lines, rebate_programs, world, rng)
    lines = add_returns(cfg, lines, cfg.months, rng)
    lines = add_credit_memos(cfg, lines, rng)
    lines = add_zero_qty_rows(cfg, lines, rng)
    lines = blank_costs(cfg, lines, rng)
    lines = reexpress_uom(cfg, lines, uom_conversions, rng)
    lines = _ensure_columns(lines)
    lines = lines.with_row_index("line_seq")
    lines, tier_table, tiered_settlements = plant_tiered_rebates(lines, rebate_programs)
    lines = add_kits(cfg, lines, items, rng)
    lines = add_free_goods(cfg, lines, rng)
    fx = build_fx_rates(cfg)
    lines = convert_lines_to_local(lines, customers, fx)

    # Stable line keys, then duplicates (verified dups share the key).
    lines = lines.with_columns(
        (pl.lit("L") + (pl.col("line_seq") + 1000001).cast(pl.String)).alias("line_key")
    )
    lines = add_duplicates(cfg, lines, rng).drop("line_seq")

    if sub_groups.height:
        sub_groups.select(
            pl.col("group_id").alias("GROUP_ID"), pl.col("item_id").alias("ITEM_NO")
        ).write_csv(erp_dir / "substitution_groups.csv")
    quotes, quote_truth = plant_quotes(cfg, world, segments)
    if quotes.height:
        quotes.write_csv(erp_dir / "quotes.csv")
    export_erp_lines(lines, erp_dir)
    if tier_table.height:
        tier_table.select(
            pl.col("rebate_program_id").alias("PGM_ID"),
            (pl.col("min_cents") / 100).alias("MIN_AMT"),
            (pl.col("rate_bp") / 100.0).alias("RATE_PCT"),
        ).write_csv(erp_dir / "rebate_program_tiers.csv")
        tiered_settlements.select(
            pl.col("customer_id").alias("CUST_NO"),
            pl.col("period").alias("PERIOD"),
            pl.col("rebate_program_id").alias("PGM_ID"),
            (pl.col("amount_cents") / 100).round(2).alias("AMT"),
        ).write_csv(erp_dir / "tiered_settlements.csv")
    if fx.height:
        fx.select(
            pl.col("currency").alias("CURR"),
            pl.col("period").alias("PERIOD"),
            (pl.col("rate_ppm") / 1_000_000).alias("USD_PER_UNIT"),
        ).write_csv(erp_dir / "fx_rates.csv")
    export_reference_tables(
        cfg,
        erp_dir,
        customers,
        items,
        list_history,
        uom_conversions,
        contracts,
        rebate_programs,
        lines,
        world,
    )
    summary = export_ground_truth(cfg, out_dir, segments, lines, world, customers)
    summary["quotes"] = quote_truth
    summary["substitution"] = {
        "groups": dict(sub_gammas.items()),
        "members": sub_groups.to_dicts() if sub_groups.height else [],
    }
    import json as _json

    (out_dir / "ground_truth" / "truth_summary.json").write_text(
        _json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


app = typer.Typer(no_args_is_help=True)


@app.command()
def main(
    out: Path = typer.Option(Path("data/synth"), help="Output directory"),  # noqa: B008
    seed: int = typer.Option(42),
    preset: Preset = typer.Option(Preset.WELL_IDENTIFIED),  # noqa: B008
    n_customers: int | None = typer.Option(None, help="Override customer count"),
    n_items: int | None = typer.Option(None, help="Override item count"),
) -> None:
    overrides: dict[str, Any] = {}
    if n_customers is not None:
        overrides["n_customers"] = n_customers
    if n_items is not None:
        overrides["n_items"] = n_items
    cfg = GeneratorConfig.for_preset(preset, seed=seed, **overrides)
    summary = generate(cfg, out)
    typer.echo(
        f"Generated {summary['n_lines_exported']} lines, "
        f"{summary['n_customers']} customers -> {out}"
    )


if __name__ == "__main__":
    app()
