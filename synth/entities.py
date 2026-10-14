"""Entity generation: calendar, customers, items, reps, planted segments,
list-price history, contracts, rebate programs.

All randomness flows from a single numpy Generator owned by the orchestrator,
so one seed fixes the whole world.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl

from synth.config import GeneratorConfig

INDUSTRIES = ["construction", "manufacturing", "oem", "facilities", "services", "retail"]
HEAVY_INDUSTRIES = {"construction", "manufacturing", "oem"}
REGIONS = ["north", "south", "east", "west"]
SIZE_BANDS = ["small", "medium", "large"]
CHANNELS = ["field", "inside", "ecommerce"]
CONTRACT_TYPES = ["none", "standard", "national"]
ITEM_ROLES = ["core", "kvi", "specialty", "long_tail"]
BASE_UOMS = ["EA", "CS", "BX"]


@dataclass(frozen=True)
class SegmentDef:
    """A planted commercial segment: a context rule plus structural parameters."""

    segment_id: int
    name: str
    rule: str  # human-readable rule, also recorded in ground truth
    elasticity: float
    base_price_index: float


def month_starts(cfg: GeneratorConfig) -> list[date]:
    out: list[date] = []
    y, m = cfg.start_year, cfg.start_month
    for _ in range(cfg.months):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def build_segments(cfg: GeneratorConfig, rng: np.random.Generator) -> list[SegmentDef]:
    """Six planted segments over customer context, plus a long-tail item override.

    Elasticities and base indexes are drawn from the configured ranges but
    spread deterministically across segments so they are well-separated.
    """
    d = cfg.demand
    n = 6
    # Evenly spaced across the range, with small seeded jitter.
    el_grid = np.linspace(d.elasticity_min, d.elasticity_max, n)
    ix_grid = np.linspace(d.base_index_min, d.base_index_max, n)
    el = el_grid + rng.uniform(-0.05, 0.05, n)
    ix = ix_grid + rng.uniform(-0.005, 0.005, n)
    specs = [
        ("national_heavy", "heavy industry AND large"),
        ("regional_heavy", "heavy industry AND NOT large"),
        ("light_large", "light industry AND large"),
        ("light_core", "light industry AND NOT large AND channel IN (field, inside)"),
        ("light_ecom", "light industry AND NOT large AND channel = ecommerce"),
        ("long_tail_items", "item role = long_tail (overrides customer rule)"),
    ]
    return [
        SegmentDef(i, name, rule, float(el[i]), float(ix[i]))
        for i, (name, rule) in enumerate(specs)
    ]


def assign_customer_segment(
    industry: np.ndarray, size_band: np.ndarray, channel: np.ndarray
) -> np.ndarray:
    """Vectorized planted segment rule for customers (segments 0..4)."""
    heavy = np.isin(industry, list(HEAVY_INDUSTRIES))
    large = size_band == "large"
    ecom = channel == "ecommerce"
    seg = np.full(industry.shape, 3, dtype=np.int64)  # light_core default
    seg[heavy & large] = 0
    seg[heavy & ~large] = 1
    seg[~heavy & large] = 2
    seg[~heavy & ~large & ecom] = 4
    return seg


def build_customers(cfg: GeneratorConfig, rng: np.random.Generator) -> pl.DataFrame:
    n = cfg.n_customers
    industry = rng.choice(INDUSTRIES, n)
    region = rng.choice(REGIONS, n)
    size_band = rng.choice(SIZE_BANDS, n, p=[0.5, 0.35, 0.15])
    channel = rng.choice(CHANNELS, n, p=[0.5, 0.3, 0.2])
    contract_type = np.where(
        rng.random(n) < cfg.contract_customer_share,
        np.where(size_band == "large", "national", "standard"),
        "none",
    )
    # ~15% of customers roll up to a shared parent account.
    n_parents = max(1, n // 20)
    parent = np.array([f"P{1000 + i}" for i in rng.integers(0, n_parents, n)])
    has_parent = rng.random(n) < 0.15
    seg = assign_customer_segment(industry, size_band, channel)
    rep = np.array([f"REP{100 + i}" for i in rng.integers(0, cfg.n_reps, n)])
    return pl.DataFrame(
        {
            "customer_id": [f"C{10000 + i}" for i in range(n)],
            "industry": industry,
            "region": region,
            "size_band": size_band,
            "channel": channel,
            "contract_type": contract_type,
            "parent_account_id": np.where(has_parent, parent, ""),
            "sales_rep_id": rep,
            "segment_id": seg,
        }
    )


def build_items(cfg: GeneratorConfig, rng: np.random.Generator) -> pl.DataFrame:
    n = cfg.n_items
    family = rng.integers(0, cfg.n_families, n)
    # Family determines category and supplier (with a little noise on supplier).
    category = family % cfg.n_categories
    supplier = np.where(
        rng.random(n) < 0.8,
        family % cfg.n_suppliers,
        rng.integers(0, cfg.n_suppliers, n),
    )
    role = rng.choice(
        ITEM_ROLES,
        n,
        p=[
            1 - cfg.long_tail_share - 0.10 - 0.10,
            0.10,
            0.10,
            cfg.long_tail_share,
        ],
    )
    service = rng.random(n) < cfg.service_item_share
    base_uom = rng.choice(BASE_UOMS, n, p=[0.7, 0.2, 0.1])
    # Base list price: lognormal across roughly $5-$500; cents.
    list_price = np.exp(rng.normal(3.5, 1.0, n))
    list_price = np.clip(list_price, 5.0, 500.0)
    list_cents = np.round(list_price * 100).astype(np.int64)
    # Margin over cost 20-45%; service items cost zero.
    margin = rng.uniform(0.20, 0.45, n)
    cost_cents = np.round(list_cents * (1 - margin)).astype(np.int64)
    cost_cents[service] = 0
    weight = np.round(np.exp(rng.normal(1.0, 0.8, n)), 2)  # lb per unit
    cube = np.round(weight * rng.uniform(0.01, 0.05, n), 4)  # ft3 per unit
    return pl.DataFrame(
        {
            "item_id": [f"I{20000 + i}" for i in range(n)],
            "item_family": [f"FAM{f:02d}" for f in family],
            "item_category": [f"CAT{c:02d}" for c in category],
            "supplier_id": [f"SUP{s:02d}" for s in supplier],
            "item_role": role,
            "is_service": service,
            "base_uom": base_uom,
            "base_list_cents": list_cents,
            "base_cost_cents": cost_cents,
            "weight_lb": weight,
            "cube_ft3": cube,
        }
    )


def build_list_price_history(
    cfg: GeneratorConfig, items: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """Effective-dated list prices: initial price at month 0, 1-3 revisions of +2-6%."""
    starts = month_starts(cfg)
    rows_item: list[str] = []
    rows_date: list[date] = []
    rows_cents: list[int] = []
    ids = items["item_id"].to_list()
    base = items["base_list_cents"].to_list()
    n_rev = rng.integers(cfg.list_revisions_min, cfg.list_revisions_max + 1, len(ids))
    for i, item_id in enumerate(ids):
        cents = int(base[i])
        rows_item.append(item_id)
        rows_date.append(starts[0])
        rows_cents.append(cents)
        rev_months = sorted(rng.choice(np.arange(3, cfg.months), int(n_rev[i]), replace=False))
        for m in rev_months:
            cents = round(cents * (1 + rng.uniform(0.02, 0.06)))
            rows_item.append(item_id)
            rows_date.append(starts[int(m)])
            rows_cents.append(cents)
    return pl.DataFrame(
        {"item_id": rows_item, "effective_date": rows_date, "list_cents": rows_cents}
    )


def build_contracts(
    cfg: GeneratorConfig,
    customers: pl.DataFrame,
    items: pl.DataFrame,
    rng: np.random.Generator,
) -> pl.DataFrame:
    """Contract pricing: fixed discount off list on 1-3 item families, full horizon.

    National contracts are price-protected (excluded from pass-through and
    price-increase recommendations downstream).
    """
    starts = month_starts(cfg)
    end = starts[-1]
    families = sorted(items["item_family"].unique().to_list())
    recs: list[dict[str, object]] = []
    cnum = 500
    for row in customers.filter(pl.col("contract_type") != "none").iter_rows(named=True):
        n_fam = int(rng.integers(1, 4))
        fams = rng.choice(families, n_fam, replace=False)
        disc = float(rng.uniform(0.08, 0.20))
        protected = row["contract_type"] == "national"
        for fam in fams:
            recs.append(
                {
                    "contract_id": f"K{cnum}",
                    "customer_id": row["customer_id"],
                    "item_family": str(fam),
                    "start_date": starts[0],
                    "end_date": end,
                    "discount_pct": round(disc, 4),
                    "price_protected": protected,
                }
            )
        cnum += 1
    if not recs:
        return pl.DataFrame(
            schema={
                "contract_id": pl.String,
                "customer_id": pl.String,
                "item_family": pl.String,
                "start_date": pl.Date,
                "end_date": pl.Date,
                "discount_pct": pl.Float64,
                "price_protected": pl.Boolean,
            }
        )
    return pl.DataFrame(recs)


def build_rebate_programs(
    cfg: GeneratorConfig, customers: pl.DataFrame, rng: np.random.Generator
) -> pl.DataFrame:
    """Customer rebate programs.

    line_accrual: rebate accrued per line and present on the export (actual).
    settlement: customer x quarter settlement totals the engine must allocate.
    """
    n = customers.height
    r = rng.random(n)
    kind = np.where(
        r < cfg.line_rebate_share,
        "line_accrual",
        np.where(r < cfg.line_rebate_share + cfg.settlement_rebate_share, "settlement", "none"),
    )
    rate_bp = np.round(rng.uniform(cfg.rebate_rate_min, cfg.rebate_rate_max, n) * 10000).astype(
        np.int64
    )
    df = pl.DataFrame(
        {
            "customer_id": customers["customer_id"],
            "program_kind": kind,
            "rate_bp": rate_bp,  # basis points, exact integer math downstream
        }
    ).filter(pl.col("program_kind") != "none")
    return df.with_columns(
        (pl.lit("RP") + pl.arange(1, df.height + 1).cast(pl.String).str.zfill(3)).alias(
            "rebate_program_id"
        )
    )


def build_uom_conversions(items: pl.DataFrame, rng: np.random.Generator) -> pl.DataFrame:
    """Alternate-unit conversions: items whose base UOM is CS or BX can also be
    expressed in EA on invoice lines; this table normalizes them back."""
    packs = {"CS": 12, "BX": 6}
    rows = [
        {
            "item_id": r["item_id"],
            "from_uom": "EA",
            "to_uom": r["base_uom"],
            "factor": packs[r["base_uom"]],
        }
        for r in items.iter_rows(named=True)
        if r["base_uom"] in packs
    ]
    return pl.DataFrame(rows)
