"""Run 4 (27.3): books tie-out. Engine monthly totals vs GL with absolute and
percentage differences; failing months flagged, never hidden."""

from __future__ import annotations

from datetime import date

import polars as pl

from margin_engine.config import Rules
from margin_engine.report.tieout import run_tieout


def _ledger() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "transaction_type": ["sale", "sale", "return", "sale", "credit"],
            "invoice_date": pl.Series(
                [
                    date(2024, 1, 10),
                    date(2024, 1, 20),
                    date(2024, 1, 25),
                    date(2024, 2, 5),
                    date(2024, 2, 6),
                ],
                dtype=pl.Date,
            ),
            "net_amount_cents": [100_000, 50_000, -10_000, 80_000, -5_000],
            "product_cost_cents": [60_000, 30_000, -6_000, None, None],
        }
    )


def test_tieout_exact_match_passes() -> None:
    gl = pl.DataFrame(
        {
            "month": ["2024-01", "2024-02"],
            "net_sales_cents": [140_000, 80_000],
            "cogs_cents": [84_000, 0],
        }
    )
    res = run_tieout(_ledger(), gl, Rules())
    assert res.n_failing == 0
    jan = res.table.filter(
        (pl.col("month") == "2024-01") & (pl.col("measure") == "net_sales")
    ).to_dicts()[0]
    assert jan["engine_cents"] == 140_000  # sales + returns; credit memo excluded
    assert jan["diff_cents"] == 0
    assert jan["within_tolerance"] is True


def test_tieout_failing_month_flagged_never_hidden() -> None:
    gl = pl.DataFrame(
        {
            "month": ["2024-01", "2024-02"],
            "net_sales_cents": [150_000, 80_000],  # Jan off by 10,000c (6.7%)
        }
    )
    res = run_tieout(_ledger(), gl, Rules())
    assert res.n_failing == 1
    jan = res.table.filter(pl.col("month") == "2024-01").to_dicts()[0]
    assert jan["within_tolerance"] is False
    assert jan["diff_cents"] == -10_000
    assert abs(jan["diff_pct"] + 6.6667) < 0.01
    # The failing month stays in the table - flagged, not dropped.
    assert res.table.filter(~pl.col("within_tolerance")).height == 1


def test_tieout_tolerance_is_configurable() -> None:
    gl = pl.DataFrame({"month": ["2024-01"], "net_sales_cents": [140_050]})  # off 50c / 0.04%
    tight = Rules.model_validate(
        {"reporting": {"tieout_tolerance_pct": 0.0001, "tieout_tolerance_cents": 10}}
    )
    loose = Rules()  # default 0.5% or 100c
    assert run_tieout(_ledger(), gl, tight).n_failing == 1
    assert run_tieout(_ledger(), gl, loose).n_failing == 0


def test_tieout_reports_cost_coverage_and_extra_months() -> None:
    gl = pl.DataFrame(
        {
            "month": ["2024-01"],  # Feb missing from the GL file
            "net_sales_cents": [140_000],
            "cogs_cents": [84_000],
            "returns_cents": [-10_000],
        }
    )
    res = run_tieout(_ledger(), gl, Rules())
    # Feb has a sale with unknown cost: coverage < 100% and stated in notes.
    assert res.cost_coverage_share < 1.0
    assert any("known cost only" in n for n in res.notes)
    assert any("absent from the GL" in n for n in res.notes)
    ret = res.table.filter(pl.col("measure") == "returns").to_dicts()[0]
    assert ret["engine_cents"] == -10_000
    assert ret["within_tolerance"] is True
