"""Run 4 (Phase 28): the committed Online Retail II sample runs offline
through the real mapping with honest no-cost handling. Pins the real-data
behaviors the case study documents: prefix-derived returns, GBP->USD
conversion from the fixture-derived fx table, anonymous-customer exclusion
from behavior views, and cost_unavailable everywhere cost would be needed."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from margin_engine.config import load_rules
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data" / "sample_online_retail"
MAPPING = REPO / "config" / "mappings" / "online_retail_ii.yaml"


@pytest.fixture(scope="module")
def result() -> PipelineResult:
    return run_pipeline(DATA, MAPPING, load_rules(REPO / "config" / "rules.yaml"))


def test_reconciles_exactly(result: PipelineResult) -> None:
    assert result.reconciliation.all_pass


def test_cancellations_are_returns(result: PipelineResult) -> None:
    types = dict(result.ledger.group_by("transaction_type").agg(pl.len()).iter_rows())
    assert types.get("return", 0) > 0
    returns = result.ledger.filter(pl.col("transaction_type") == "return")
    assert returns["invoice_id"].str.starts_with("C").all()
    # Returns never appear in the behavior view.
    assert (result.observations["transaction_type"] == "sale").all()


def test_gbp_converted_to_usd(result: PipelineResult) -> None:
    assert "net_amount_cents_src" in result.ledger.columns  # original GBP kept
    rates = result.ledger["fx_rate_ppm"].unique().to_list()
    assert all(1_000_000 < r < 2_500_000 for r in rates)  # GBP/USD in [1.0, 2.5]
    assert any("converted" in n and "USD" in n for n in result.notes)


def test_no_cost_is_declared_not_invented(result: PipelineResult) -> None:
    assert result.ledger["product_cost_cents"].null_count() == result.ledger.height
    assert result.ledger["pocket_gp_cents"].null_count() == result.ledger.height
    assert any("cost_unavailable" in n for n in result.notes)


def test_anonymous_customers_stay_in_ledger_not_observations(
    result: PipelineResult,
) -> None:
    anon_sales = result.ledger.filter(
        (pl.col("transaction_type") == "sale") & pl.col("customer_id").is_null()
    )
    assert anon_sales.height > 0  # the sample contains guest sales
    assert result.observations["customer_id"].null_count() == 0
    # Their dollars are still in the ledger total (reconciliation covers this,
    # but assert the direct sum relation too).
    total = int(
        result.ledger.filter(pl.col("transaction_type") == "sale")["net_amount_cents"].sum()
    )
    obs_total = int(result.observations["net_amount_cents"].sum())
    assert total > obs_total


def test_duplicates_flagged_never_removed(result: PipelineResult) -> None:
    # The export has no stable line key, so true source duplicates surface as
    # look-alike duplicates: flagged, never removed (5.3).
    assert result.removed_duplicates.height == 0
    assert int(result.ledger["flag_lookalike_duplicate"].sum()) > 0


def test_full_run_completes_on_no_cost_no_list_export(tmp_path: Path) -> None:
    """End-to-end on the sample: segmentation, guidance (bands only, no
    floors), level-2 opportunities on the labeled index-fallback price basis,
    cost-dependent levels null, report rendered."""
    from margin_engine.report.full_run import run_full

    res = run_full(DATA, MAPPING, REPO / "config" / "rules.yaml", tmp_path)
    assert res.pipeline.reconciliation.all_pass
    o = res.opportunities
    assert o.height > 0
    assert (o["price_basis"] == "index_fallback_item_median").all()
    assert o["expected_contribution_change_cents"].null_count() == o.height
    assert bool(o["cost_unavailable"].all())
    # No instruments and a short window: elasticity never above descriptive.
    assert set(res.sensitivity["status"].to_list()) <= {
        "insufficient_data",
        "descriptive_elasticity",
    }
    assert (tmp_path / "report.html").exists()
