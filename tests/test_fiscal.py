"""Phase 36: fiscal calendars — fixtures for each calendar type, year
boundaries, 53-week years, the custom period table, and the books tie-out on a
custom calendar. The default calendar must reproduce the engine's historical
period strings exactly (byte-identity of all exports is checked separately)."""

from __future__ import annotations

import datetime as dt
from itertools import pairwise
from pathlib import Path

import polars as pl
import pytest
import yaml

from margin_engine.config import FiscalConfig, load_rules
from margin_engine.fiscal import (
    FiscalCalendarError,
    build_calendar,
    week_year_periods,
    year_end,
)
from margin_engine.report.tieout import run_tieout

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample" / "erp"
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"
RULES = REPO / "config" / "rules.yaml"

SATURDAY = 5


def test_calendar_months_matches_legacy_expressions() -> None:
    cal = build_calendar(FiscalConfig())
    df = pl.DataFrame({"d": [dt.date(2024, 1, 1), dt.date(2024, 3, 31), dt.date(2025, 12, 15)]})
    out = cal.attach(df, "d", idx="idx", period="period", quarter="quarter")
    assert out["idx"].to_list() == [2024 * 12 + 1, 2024 * 12 + 3, 2025 * 12 + 12]
    assert out["period"].to_list() == ["2024-01", "2024-03", "2025-12"]
    assert out["quarter"].to_list() == ["2024Q1", "2024Q1", "2025Q4"]


def test_year_end_nearest_weekday() -> None:
    # Dec 31 2022 was a Saturday: the Saturday nearest Dec 31 is itself.
    assert year_end(2022, 12, SATURDAY) == dt.date(2022, 12, 31)
    # Dec 31 2023 was a Sunday: the nearest Saturday is Dec 30.
    assert year_end(2023, 12, SATURDAY) == dt.date(2023, 12, 30)
    # Jan-end convention (NRF style): Saturday nearest Jan 31.
    assert year_end(2024, 1, SATURDAY).weekday() == SATURDAY


@pytest.mark.parametrize(
    ("kind", "pattern"),
    [("4-4-5", (4, 4, 5)), ("4-5-4", (4, 5, 4)), ("5-4-4", (5, 4, 4))],
)
def test_week_calendar_period_lengths_and_boundaries(
    kind: str, pattern: tuple[int, int, int]
) -> None:
    rows = week_year_periods(2023, pattern, 12, SATURDAY)
    assert len(rows) == 12
    weeks = [((end - start).days + 1) // 7 for start, end, *_ in rows]
    expected = [w for _ in range(4) for w in pattern]
    total = (year_end(2023, 12, SATURDAY) - year_end(2022, 12, SATURDAY)).days // 7
    if total == 53:
        expected[-1] += 1
    assert weeks == expected
    # Contiguous periods, and the year starts the day after the prior year ends.
    assert rows[0][0] == year_end(2022, 12, SATURDAY) + dt.timedelta(days=1)
    assert rows[-1][1] == year_end(2023, 12, SATURDAY)
    for (_, prev_end, *_), (nxt_start, *_rest) in pairwise(rows):
        assert nxt_start == prev_end + dt.timedelta(days=1)
    # Labels and quarters.
    assert rows[0][3] == "FY2023 P01"
    assert rows[0][4] == "FY2023 Q1"
    assert rows[11][4] == "FY2023 Q4"


def test_53_week_year_extends_final_period() -> None:
    # Find a 53-week fiscal year under the Saturday / December convention.
    fy53 = None
    for y in range(2015, 2035):
        weeks = (year_end(y, 12, SATURDAY) - year_end(y - 1, 12, SATURDAY)).days // 7
        if weeks == 53:
            fy53 = y
            break
    assert fy53 is not None, "no 53-week year found in 2015-2034 (convention broken?)"
    rows = week_year_periods(fy53, (4, 4, 5), 12, SATURDAY)
    weeks = [((end - start).days + 1) // 7 for start, end, *_ in rows]
    assert sum(weeks) == 53
    assert weeks[:-1] == [4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 4]
    assert weeks[-1] == 6  # the 53rd week extends the year's final period


def test_week_calendar_attach_year_boundary() -> None:
    cfg = FiscalConfig(calendar="4-4-5")
    cal = build_calendar(cfg)
    # year_end(2023) = 2023-12-30 (Saturday); the next day starts FY2024.
    df = pl.DataFrame({"d": [dt.date(2023, 12, 30), dt.date(2023, 12, 31)]})
    out = cal.attach(df, "d", period="p", quarter="q", idx="i")
    assert out["p"].to_list() == ["FY2023 P12", "FY2024 P01"]
    assert out["q"].to_list() == ["FY2023 Q4", "FY2024 Q1"]
    # Consecutive period indexes across the year boundary.
    assert out["i"].to_list()[1] == out["i"].to_list()[0] + 1


def _custom_table() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "period_label": ["P1", "P2"],
            "start_date": [dt.date(2024, 1, 1), dt.date(2024, 2, 5)],
            "end_date": [dt.date(2024, 2, 4), dt.date(2024, 3, 3)],
            "quarter_label": ["Q1", "Q1"],
        }
    )


def test_custom_calendar_attach_and_errors() -> None:
    cfg = FiscalConfig(calendar="custom")
    cal = build_calendar(cfg, _custom_table())
    df = pl.DataFrame({"d": [dt.date(2024, 1, 15), dt.date(2024, 2, 5)]})
    out = cal.attach(df, "d", period="p", quarter="q")
    assert out["p"].to_list() == ["P1", "P2"]
    assert out["q"].to_list() == ["Q1", "Q1"]
    # A date outside every period is an actionable error, never a guess.
    with pytest.raises(FiscalCalendarError, match="outside"):
        cal.attach(pl.DataFrame({"d": [dt.date(2025, 1, 1)]}), "d", period="p")
    # Missing table and overlapping periods fail at build.
    with pytest.raises(FiscalCalendarError, match="fiscal_periods"):
        build_calendar(cfg, None)
    overlapping = _custom_table().with_columns(
        pl.Series("start_date", [dt.date(2024, 1, 1), dt.date(2024, 2, 1)])
    )
    with pytest.raises(FiscalCalendarError, match="overlap"):
        build_calendar(cfg, overlapping)


def test_custom_calendar_quarter_fallback_noted() -> None:
    cal = build_calendar(FiscalConfig(calendar="custom"), _custom_table().drop("quarter_label"))
    assert any("quarter" in n for n in cal.notes)
    out = cal.attach(pl.DataFrame({"d": [dt.date(2024, 1, 2)]}), "d", quarter="q")
    assert out["q"].to_list() == ["P1"]


def test_tieout_on_custom_calendar_exact() -> None:
    rules = load_rules(RULES)
    cal = build_calendar(FiscalConfig(calendar="custom"), _custom_table())
    ledger = pl.DataFrame(
        {
            "transaction_type": ["sale", "sale", "return"],
            "invoice_date": [dt.date(2024, 1, 10), dt.date(2024, 2, 10), dt.date(2024, 2, 20)],
            "net_amount_cents": [10_000, 20_000, -5_000],
            "product_cost_cents": [6_000, 12_000, -3_000],
        }
    )
    gl = pl.DataFrame(
        {
            "month": ["P1", "P2"],
            "net_sales_cents": [10_000, 15_000],
            "cogs_cents": [6_000, 9_000],
        }
    )
    res = run_tieout(ledger, gl, rules, cal)
    assert res.n_failing == 0
    assert set(res.table["month"].to_list()) == {"P1", "P2"}
    # A genuinely different GL figure is flagged, never hidden.
    gl_bad = gl.with_columns(pl.Series("net_sales_cents", [10_000, 99_999]))
    assert run_tieout(ledger, gl_bad, rules, cal).n_failing >= 1


def test_full_pipeline_on_445_calendar(tmp_path: Path) -> None:
    """The sample world runs end to end on a 4-4-5 calendar: fiscal quarters in
    peer groups, fiscal panel periods, reconciliation still exact."""
    from margin_engine.report.full_run import run_full

    raw = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    raw["fiscal"] = {"calendar": "4-4-5", "year_end_month": 12, "year_end_weekday": "saturday"}
    rules_path = tmp_path / "rules_445.yaml"
    rules_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    result = run_full(SAMPLE, MAPPING, rules_path, tmp_path / "out", validate=False)
    assert result.pipeline.reconciliation.all_pass
    quarters = result.flagged["quarter"].drop_nulls().unique().to_list()
    assert quarters and all(q.startswith("FY") and " Q" in q for q in quarters)
    assert result.panel is not None
    periods = result.panel["period"].drop_nulls().unique().to_list()
    assert periods and all(p.startswith("FY") and " P" in p for p in periods)
    assert any("fiscal calendar '4-4-5'" in n for n in result.pipeline.notes)
