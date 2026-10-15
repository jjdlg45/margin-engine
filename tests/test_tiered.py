"""Phase 11 verification: tiered retrospective rebates — hand fixtures, accrual
vs settlement reconciliation, planted tier outcomes recovered."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline
from tests.fixtures.builder import FixtureWorld

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def _tiered_world(tmp_path: Path, *, settle: bool) -> FixtureWorld:
    """Customer on a 1%/2% tier program (threshold $1000): two $600 sales.
    Accruals: line1 cum 600 -> 1% = $6.00; line2 cum 1200 -> 2% = $12.00.
    Retrospective settlement: 2% x 1200 = $24.00; true-up +$6.00."""
    w = FixtureWorld(tmp_path)
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "60.00")
    for i, dt in enumerate(["02/10/2024", "07/10/2024"]):
        w.add_line(
            INV_NO=f"INV{i}",
            CUST_NO="C1",
            ITEM_NO="X1",
            QTY_SHP=10,
            UNIT_PRC="60.00",
            EXT_NET="600.00",
            LIST_PRC="60.00",
            UNIT_CST="30.0000",
            INV_DT=dt,
        )
    w.rebate_programs = [{"PGM_ID": "TIER01", "CUST_NO": "C1", "KIND": "tiered", "RATE_PCT": "0.0"}]
    w.rebate_program_tiers = [
        {"PGM_ID": "TIER01", "MIN_AMT": "0.00", "RATE_PCT": "1.0"},
        {"PGM_ID": "TIER01", "MIN_AMT": "1000.00", "RATE_PCT": "2.0"},
    ]
    if settle:
        w.tiered_settlements = [
            {"CUST_NO": "C1", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "24.00"}
        ]
    return w


def test_tiered_accrual_estimated_when_unsettled(tmp_path: Path) -> None:
    w = _tiered_world(tmp_path, settle=False)
    res = run_pipeline(w.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    rows = {r["source_line_key"]: r for r in res.ledger.to_dicts()}
    assert rows["L1"]["tiered_rate_bp"] == 100
    assert rows["L2"]["tiered_rate_bp"] == 200
    assert rows["L1"]["tiered_accrued_cents"] == 600
    assert rows["L2"]["tiered_accrued_cents"] == 1_200
    for k in ("L1", "L2"):
        assert rows[k]["tiered_rebate_status"] == "estimated"
        assert rows[k]["tiered_rebate_used_cents"] == rows[k]["tiered_accrued_cents"]
    # Pocket reduced by the accrual: 600.00 - 6.00 on line 1.
    assert rows["L1"]["pocket_revenue_cents"] == 60_000 - 600


def test_tiered_settlement_trues_up_to_actual(tmp_path: Path) -> None:
    w = _tiered_world(tmp_path, settle=True)
    res = run_pipeline(w.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    rows = {r["source_line_key"]: r for r in res.ledger.to_dicts()}
    # Actual $24.00 allocated by net share: $12.00 each (equal nets).
    for k in ("L1", "L2"):
        assert rows[k]["tiered_rebate_status"] == "actual"
        assert rows[k]["tiered_actual_alloc_cents"] == 1_200
        assert rows[k]["tiered_rebate_used_cents"] == 1_200
        # Accruals stay visible next to the actual.
        assert rows[k]["tiered_accrued_cents"] in (600, 1_200)
    assert rows["L1"]["pocket_revenue_cents"] == 60_000 - 1_200
    # True-up reported: 2400 actual - 1800 accrued = +600.
    assert res.reconciliation.residuals["tiered_true_up_cents"] == 600
    check = next(
        c for c in res.reconciliation.checks if c["check"] == "tiered_settlement_allocation"
    )
    assert check["allocated_cents"] == 2_400
    assert check["residual_cents"] == 0
    assert check["source_total_cents"] == 2_400


def test_tiered_settlement_without_lines_is_residual(tmp_path: Path) -> None:
    w = _tiered_world(tmp_path, settle=True)
    w.tiered_settlements.append(
        {"CUST_NO": "C9", "PERIOD": "2024-FY", "PGM_ID": "TIER01", "AMT": "5.00"}
    )
    res = run_pipeline(w.write(), MAPPING, Rules())
    assert res.reconciliation.all_pass
    assert res.reconciliation.residuals["tiered_settlements_unallocated_cents"] == 500


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PipelineResult]:
    out = tmp_path_factory.mktemp("tiered")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=4, n_customers=100, n_items=220)
    generate(cfg, out)
    res = run_pipeline(out / "erp", MAPPING, load_rules(REPO / "config" / "rules.yaml"))
    return out, res


def test_planted_tier_outcomes_recovered(generated: tuple[Path, PipelineResult]) -> None:
    """Per-line tier rates and accruals match the planted truth exactly on
    customer-years free of ambiguity (planted look-alike duplicates shift the
    engine's cumulative basis by design: flagged uncertain rows accrue as
    genuine until resolved, while planted truth predates the copies). The
    contaminated share must stay small and is asserted explicitly."""
    out, res = generated
    truth = pl.read_parquet(out / "ground_truth" / "line_truth.parquet")
    led = res.ledger.with_columns(pl.col("invoice_date").dt.year().alias("_year"))
    contaminated = (
        led.filter(pl.col("flag_lookalike_duplicate") | pl.col("flag_key_collision"))
        .select("customer_id", "_year")
        .unique()
    )
    j = led.join(
        truth.select("line_key", "truth_tiered_accrual_cents", "truth_tiered_rate_bp"),
        left_on="source_line_key",
        right_on="line_key",
        how="inner",
    ).filter(pl.col("tiered_rate_bp") > 0)
    assert j.height > 100
    clean = j.join(contaminated, on=["customer_id", "_year"], how="anti")
    assert clean.height > 100
    # Every mismatching line sits in a contaminated customer-year.
    mismatches = j.filter(pl.col("tiered_rate_bp") != pl.col("truth_tiered_rate_bp"))
    outside = mismatches.join(contaminated, on=["customer_id", "_year"], how="anti")
    assert outside.height == 0
    assert clean.filter(pl.col("tiered_rate_bp") != pl.col("truth_tiered_rate_bp")).height == 0
    assert (
        clean.select(
            (pl.col("tiered_accrued_cents") - pl.col("truth_tiered_accrual_cents")).abs().sum()
        ).item()
        == 0
    )
    # All planted tier rates occur somewhere (outcomes genuinely differ).
    assert set(j["tiered_rate_bp"].unique().to_list()) == {100, 200, 300}


def test_accrual_vs_settlement_reconciles(generated: tuple[Path, PipelineResult]) -> None:
    out, res = generated
    settles = pl.read_csv(out / "erp" / "tiered_settlements.csv")
    total_settle = round(settles["AMT"].sum() * 100)
    used_actual = int(
        res.ledger.filter(pl.col("tiered_rebate_status") == "actual")[
            "tiered_actual_alloc_cents"
        ].sum()
    )
    resid = res.reconciliation.residuals.get("tiered_settlements_unallocated_cents", 0)
    assert used_actual + resid == total_settle
    # True-up equals actual minus accrued on settled lines (reported).
    settled = res.ledger.filter(pl.col("tiered_rebate_status") == "actual")
    expect_true_up = int(settled["tiered_actual_alloc_cents"].sum()) - int(
        settled["tiered_accrued_cents"].sum()
    )
    assert res.reconciliation.residuals["tiered_true_up_cents"] == expect_true_up
