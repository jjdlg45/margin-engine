"""Phase 2 recovery vs. planted truth (section 8 targets).

- Actual amounts (line rebates, concessions, billed freight, vendor rebates): exact.
- Allocated amounts (settlement rebates, freight expense): within ±5% of planted.
Runs the full pipeline against a generated world.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline, store_duckdb

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PipelineResult]:
    out = tmp_path_factory.mktemp("recovery")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=11, n_customers=80, n_items=250)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, Rules())
    return out, res


def _truth(out: Path) -> pl.DataFrame:
    return pl.read_parquet(out / "ground_truth" / "line_truth.parquet")


def _joined(out: Path, res: PipelineResult) -> pl.DataFrame:
    return res.ledger.join(_truth(out), left_on="source_line_key", right_on="line_key", how="inner")


def test_reconciliation_passes_on_generated_world(
    world: tuple[Path, PipelineResult],
) -> None:
    _, res = world
    assert res.reconciliation.all_pass


def test_verified_duplicates_removed_lookalikes_kept(
    world: tuple[Path, PipelineResult],
) -> None:
    out, res = world
    truth = _truth(out)
    n_verified_keys = truth.filter(pl.col("truth_dup_type") == "verified_source").height
    assert res.removed_duplicates.height == n_verified_keys
    # Look-alike copies kept and flagged.
    lookalikes = truth.filter(pl.col("truth_dup_type") == "lookalike_copy")
    joined = res.ledger.join(
        lookalikes.select("line_key"), left_on="source_line_key", right_on="line_key", how="semi"
    )
    assert joined.height == lookalikes.height
    assert joined["flag_lookalike_duplicate"].all()


def test_line_rebates_exact(world: tuple[Path, PipelineResult]) -> None:
    out, res = world
    j = _joined(out, res)
    diff = j.select(
        (pl.col("rebate_actual_cents") - pl.col("truth_line_rebate_cents")).abs().sum()
    ).item()
    assert diff == 0


def test_freight_concession_exact(world: tuple[Path, PipelineResult]) -> None:
    out, res = world
    j = _joined(out, res)
    diff = j.select(
        (pl.col("freight_concession_cents") - pl.col("truth_freight_concession_cents")).abs().sum()
    ).item()
    assert diff == 0


def test_vendor_rebates_exact_where_cost_known(world: tuple[Path, PipelineResult]) -> None:
    out, res = world
    j = _joined(out, res).filter((pl.col("transaction_type") == "sale") & ~pl.col("cost_missing"))
    diff = j.select(
        (pl.col("vendor_rebate_cents") - pl.col("truth_vendor_rebate_cents")).abs().sum()
    ).item()
    assert diff == 0


def test_settlement_rebates_allocated_within_5pct(
    world: tuple[Path, PipelineResult],
) -> None:
    out, res = world
    j = _joined(out, res).filter(
        (pl.col("transaction_type") == "sale") & (pl.col("truth_settlement_rebate_cents") > 0)
    )
    per_customer = j.group_by("customer_id").agg(
        pl.col("rebate_settlement_alloc_cents").sum().alias("alloc"),
        pl.col("truth_settlement_rebate_cents").sum().alias("truth"),
    )
    worst = per_customer.select(
        ((pl.col("alloc") - pl.col("truth")).abs() / pl.col("truth")).max()
    ).item()
    assert worst <= 0.05, f"settlement allocation off by {worst:.2%} for some customer"


def test_freight_expense_allocated_within_5pct(
    world: tuple[Path, PipelineResult],
) -> None:
    out, res = world
    j = _joined(out, res).filter(
        (pl.col("transaction_type") == "sale") & (pl.col("truth_freight_expense_cents") > 0)
    )
    # Customer-level aggregates (order totals are exact by construction; the
    # fixed-vs-variable split makes line attribution approximate).
    per_customer = (
        j.group_by("customer_id")
        .agg(
            pl.col("freight_expense_alloc_cents").sum().alias("alloc"),
            pl.col("truth_freight_expense_cents").sum().alias("truth"),
        )
        .filter(pl.col("truth") > 10_000)  # ratios on tiny bases are noise
    )
    worst = per_customer.select(
        ((pl.col("alloc") - pl.col("truth")).abs() / pl.col("truth")).max()
    ).item()
    assert worst <= 0.05, f"freight allocation off by {worst:.2%} for some customer"


def test_billed_freight_exact_at_order_level(world: tuple[Path, PipelineResult]) -> None:
    out, res = world
    charges = pl.read_csv(out / "erp" / "order_charges.csv")
    engine_orders = (
        res.ledger.filter(pl.col("transaction_type") == "sale")
        .group_by("order_id")
        .agg(pl.col("billed_charge_alloc_cents").sum().alias("alloc"))
    )
    j = charges.with_columns((pl.col("AMT") * 100).round(0).cast(pl.Int64).alias("cents")).join(
        engine_orders, left_on="ORD_NO", right_on="order_id", how="left"
    )
    diff = j.select((pl.col("cents") - pl.col("alloc")).abs().sum()).item()
    assert diff == 0


def test_missing_cost_share_reported(world: tuple[Path, PipelineResult]) -> None:
    out, res = world
    truth = _truth(out)
    n_truth = truth.filter(pl.col("cost_missing")).height
    n_engine = res.ledger.filter(pl.col("flag_missing_cost")).height
    assert n_engine == n_truth
    # Zero-cost service lines flagged separately, never as missing.
    svc = res.ledger.filter(pl.col("flag_zero_cost"))
    assert svc.height > 0
    assert not svc["flag_missing_cost"].any()


def test_duckdb_store_roundtrip(world: tuple[Path, PipelineResult], tmp_path: Path) -> None:
    import duckdb

    _out, res = world
    db = tmp_path / "engine.duckdb"
    store_duckdb(res, db)
    con = duckdb.connect(str(db))
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        assert {
            "financial_ledger",
            "sales_observations",
            "removed_duplicates",
            "reconciliation_report",
            "reconciliation_residuals",
        } <= tables
        n = con.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0]
        assert n == res.ledger.height
        # Audit trail: every ledger row carries its source line key.
        nk = con.execute(
            "SELECT COUNT(*) FROM financial_ledger WHERE source_line_key IS NULL"
        ).fetchone()[0]
        assert nk == 0
    finally:
        con.close()


def test_return_economics_recovered(world: tuple[Path, PipelineResult]) -> None:
    """Planted return reasons, restocking fees and return freight recover
    exactly (they are exported, so the engine must treat them as actuals)."""
    out, res = world
    truth = _truth(out)
    rets = res.ledger.filter(pl.col("transaction_type") == "return")
    j = rets.join(truth, left_on="source_line_key", right_on="line_key", how="inner")
    assert j.height > 0
    # Reasons mapped to canonical values.
    mapped = {
        "CUST_ERR": "customer_error",
        "DIST_ERR": "distributor_error",
        "DEFECT": "defective",
    }
    mismatch = j.filter(
        pl.col("return_reason")
        != pl.col("truth_return_reason_code").replace_strict(mapped, default="unspecified")
    )
    assert mismatch.height == 0
    # Recovery = planted fee + planted billed freight, exact.
    diff = j.select(
        (
            pl.col("return_recovery_cents")
            - pl.col("truth_restock_fee_cents")
            - pl.col("truth_rtn_frt_billed_cents")
        )
        .abs()
        .sum()
    ).item()
    assert diff == 0
    assert (j["return_recovery_status"] == "actual").all()
    # Return freight expense from the log, exact per line (single-line returns)
    # and conserved per invoice in general.
    per_inv = j.group_by("invoice_id").agg(
        pl.col("return_freight_exp_cents").sum().alias("alloc"),
        pl.col("truth_rtn_frt_exp_cents").sum().alias("truth"),
    )
    assert per_inv.filter(pl.col("alloc") != pl.col("truth")).height == 0
