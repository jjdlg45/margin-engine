"""Quotes with planted win-probability curves (Phase 16).

Per segment, P(win | quote) = sigmoid(a_s + b_s x quote_price_index) with a
planted negative price coefficient b_s. Won quotes close below the quote by a
planted negotiation gap. Quotes ride their own RNG stream (seed + 888), so
enabling them never perturbs the main world.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from synth.config import GeneratorConfig
from synth.demand import DemandWorld
from synth.entities import SegmentDef, month_starts
from synth.money import div_round_half_even

QUOTES_PER_PAIR = 1.2  # expected quotes per active pair over the horizon
NEGOTIATION_GAP_MEAN = 0.03  # won deals close ~3% below quote
WIN_B_RANGE = (-8.0, -4.0)  # planted price coefficient range


def plant_quotes(
    cfg: GeneratorConfig,
    world: DemandWorld,
    segments: list[SegmentDef],
) -> tuple[pl.DataFrame, dict[str, object]]:
    """Returns (quotes frame, truth dict with per-segment win-curve params)."""
    rng = np.random.default_rng(cfg.seed + 888)
    starts = month_starts(cfg)

    b_seg = {s.segment_id: float(rng.uniform(*WIN_B_RANGE)) for s in segments}
    # Calibrate the intercept so a quote at the segment's typical index wins
    # ~50% of the time: a = -b x (base_index + 0.05).
    a_seg = {s.segment_id: -b_seg[s.segment_id] * (s.base_price_index + 0.05) for s in segments}

    pairs = world.pairs
    n_pairs = pairs.height
    n_quotes = rng.poisson(QUOTES_PER_PAIR, n_pairs)
    rep = np.repeat(np.arange(n_pairs), n_quotes)
    total = len(rep)
    if total == 0:
        return pl.DataFrame(), {"win_curve": {}}

    seg = pairs["segment_id"].to_numpy()[rep]
    base_ix = pairs["base_index"].to_numpy()[rep]
    pair_eff = pairs["pair_eff"].to_numpy()[rep]
    month = rng.integers(0, cfg.months, total)

    # Quote index: around the pair's level, slightly above, with real spread.
    idx_q = base_ix * np.exp(pair_eff) * np.exp(rng.normal(0.05, 0.07, total))
    a = np.array([a_seg[s] for s in seg])
    b = np.array([b_seg[s] for s in seg])
    p_win = 1.0 / (1.0 + np.exp(-(a + b * idx_q)))
    won = rng.random(total) < p_win
    gap = np.clip(rng.normal(NEGOTIATION_GAP_MEAN, 0.01, total), 0.0, 0.10)

    # Prices in cents off the item's base list (reporting currency).
    # Use the listed price in force at the quote month via the panel's list.
    lists = world.panel.select("customer_id", "item_id", "month", "list_cents").unique(
        subset=["customer_id", "item_id", "month"]
    )
    qdf = pl.DataFrame(
        {
            "customer_id": pairs["customer_id"].to_numpy()[rep],
            "item_id": pairs["item_id"].to_numpy()[rep],
            "month": month,
            "segment_id": seg,
            "idx_q": idx_q,
            "won": won,
            "gap": gap,
        }
    ).join(lists, on=["customer_id", "item_id", "month"], how="left")
    # Deterministic row order BEFORE any positional RNG draw: join output order
    # is not guaranteed, and qty/date draws below align to rows by position.
    qdf = qdf.filter(pl.col("list_cents").is_not_null()).sort(
        "customer_id", "item_id", "month", "idx_q"
    )
    lc = qdf["list_cents"].to_numpy()
    quote_cents = div_round_half_even(
        np.round(lc * qdf["idx_q"].to_numpy() * 10_000).astype(np.int64), 10_000
    )
    close_cents = div_round_half_even(
        np.round(quote_cents * (1 - qdf["gap"].to_numpy()) * 10_000).astype(np.int64), 10_000
    )
    qty = np.maximum(1, rng.poisson(12, qdf.height))
    dates = [
        starts[m].replace(day=int(d))
        for m, d in zip(qdf["month"].to_list(), rng.integers(1, 28, qdf.height), strict=True)
    ]
    quotes = qdf.with_columns(
        pl.Series("QUOTE_DT", [d.strftime("%m/%d/%Y") for d in dates]),
        pl.Series("QTY", qty.astype(np.int64)),
        pl.Series("QUOTE_PRC", (quote_cents / 100.0).round(2)),
        pl.Series("_close", (close_cents / 100.0).round(2)),
        pl.when(pl.col("won")).then(pl.lit("W")).otherwise(pl.lit("L")).alias("OUTCOME"),
    ).with_columns(pl.when(pl.col("won")).then(pl.col("_close")).otherwise(None).alias("CLOSE_PRC"))
    truth = {
        "win_curve": {
            str(s.segment_id): {"a": a_seg[s.segment_id], "b": b_seg[s.segment_id]}
            for s in segments
        },
        "negotiation_gap_mean": NEGOTIATION_GAP_MEAN,
        "n_quotes": quotes.height,
    }
    export = quotes.select(
        pl.col("QUOTE_DT"),
        pl.col("customer_id").alias("CUST_NO"),
        pl.col("item_id").alias("ITEM_NO"),
        pl.col("QTY"),
        pl.col("QUOTE_PRC"),
        pl.col("CLOSE_PRC"),
        pl.col("OUTCOME"),
    )
    return export, truth
