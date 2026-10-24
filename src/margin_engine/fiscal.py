"""Fiscal calendars (Run 5, Phase 36).

One object answers every "which period is this date in" question for the
engine's ANALYTIC periods: the sensitivity panel, peer-group quarters, the
guidance observation unit, baseline windows, period counts and the books
tie-out. Data-declared periods (rebate settlement periods, landed-cost months,
FX rate months) always follow the source data and never pass through here.

Kinds:
- calendar_months: the default. attach() uses the exact expressions the engine
  has always used (period "%Y-%m", quarter "YYYYQn", index year*12+month), so
  every output is byte-identical to the pre-fiscal-calendar engine.
- 4-4-5 / 4-5-4 / 5-4-4: retail week calendars. A fiscal year is the 52 or 53
  weeks ending on the configured weekday nearest to the last day of the
  configured year-end month; each quarter holds the pattern's weeks and a
  53-week year extends the final period of the year (documented convention).
  Labels: "FY2026 P03", quarters "FY2026 Q1". The period index is
  fiscal_year*12 + period - 1, consecutive across years.
- custom: an explicit period table (reference table `fiscal_periods`:
  period_label, start_date, end_date, optional quarter_label). Periods must
  not overlap; a data date outside every period is an actionable error, and a
  missing quarter_label falls back to the period label (stated in the notes).

Every monetary and behavioral number continues to come from the same rows;
only the grouping labels change with the calendar.
"""

from __future__ import annotations

import datetime as dt
from calendar import monthrange
from dataclasses import dataclass

import polars as pl

from margin_engine.config import FiscalConfig

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_PATTERNS = {
    "4-4-5": (4, 4, 5),
    "4-5-4": (4, 5, 4),
    "5-4-4": (5, 4, 4),
}


class FiscalCalendarError(ValueError):
    """The configured calendar cannot place a date (actionable, never silent)."""


def year_end(year: int, year_end_month: int, weekday: int) -> dt.date:
    """The configured weekday nearest to the last day of year_end_month."""
    last = dt.date(year, year_end_month, monthrange(year, year_end_month)[1])
    delta = (weekday - last.weekday() + 7) % 7
    forward = last + dt.timedelta(days=delta)
    backward = forward - dt.timedelta(days=7)
    return forward if (forward - last).days <= (last - backward).days else backward


def week_year_periods(
    fiscal_year: int, pattern: tuple[int, int, int], year_end_month: int, weekday: int
) -> list[tuple[dt.date, dt.date, int, str, str]]:
    """(start, end, period_idx, period_label, quarter_label) for one fiscal
    year; a 53rd week extends the year's final period."""
    end = year_end(fiscal_year, year_end_month, weekday)
    start = year_end(fiscal_year - 1, year_end_month, weekday) + dt.timedelta(days=1)
    n_weeks = ((end - start).days + 1) // 7
    weeks = [w for _q in range(4) for w in pattern]
    if n_weeks == 53:
        weeks[-1] += 1
    elif n_weeks != 52:  # defensive: the anchor rule only yields 52 or 53
        msg = f"fiscal year {fiscal_year} spans {n_weeks} weeks; expected 52 or 53"
        raise FiscalCalendarError(msg)
    rows = []
    cursor = start
    for i, w in enumerate(weeks):
        p_start = cursor
        p_end = cursor + dt.timedelta(days=7 * w - 1)
        cursor = p_end + dt.timedelta(days=1)
        rows.append(
            (
                p_start,
                p_end,
                fiscal_year * 12 + i,
                f"FY{fiscal_year} P{i + 1:02d}",
                f"FY{fiscal_year} Q{i // 3 + 1}",
            )
        )
    return rows


@dataclass(frozen=True)
class FiscalCalendar:
    kind: str
    config: FiscalConfig
    # Non-month calendars carry a period table: start, end, period_idx,
    # period, quarter (custom tables are validated and normalized at build).
    table: pl.DataFrame | None = None
    notes: tuple[str, ...] = ()

    @property
    def is_calendar_months(self) -> bool:
        return self.kind == "calendar_months"

    def _table_covering(self, lo: dt.date, hi: dt.date) -> pl.DataFrame:
        if self.kind == "custom":
            if self.table is None:  # guarded at build; defensive here
                msg = "custom fiscal calendar without a fiscal_periods table"
                raise FiscalCalendarError(msg)
            t = self.table
            t_lo = t["start"].min()
            t_hi = t["end"].max()
            if lo < t_lo or hi > t_hi:  # type: ignore[operator]
                msg = (
                    f"dates {lo}..{hi} fall outside the custom fiscal_periods "
                    f"table ({t_lo!s}..{t_hi!s}). Extend the table; the engine "
                    f"never invents periods."
                )
                raise FiscalCalendarError(msg)
            return t
        pattern = _PATTERNS[self.kind]
        weekday = _WEEKDAYS[self.config.year_end_weekday]
        rows: list[tuple[dt.date, dt.date, int, str, str]] = []
        fy = lo.year  # a fiscal year containing lo ends in lo.year or lo.year+1
        while year_end(fy, self.config.year_end_month, weekday) < lo:
            fy += 1
        while True:
            year_rows = week_year_periods(fy, pattern, self.config.year_end_month, weekday)
            rows.extend(year_rows)
            if year_rows[-1][1] >= hi:
                break
            fy += 1
        return pl.DataFrame(
            {
                "start": [r[0] for r in rows],
                "end": [r[1] for r in rows],
                "period_idx": [r[2] for r in rows],
                "period": [r[3] for r in rows],
                "quarter": [r[4] for r in rows],
            },
            schema={
                "start": pl.Date,
                "end": pl.Date,
                "period_idx": pl.Int64,
                "period": pl.String,
                "quarter": pl.String,
            },
        )

    def index_table(self, lo: dt.date, hi: dt.date) -> pl.DataFrame:
        """The period table (start, end, period_idx, period, quarter) covering
        [lo, hi] — for consumers that work on period indexes without dates
        (the panel scaffold, instrument-table joins). calendar_months builds
        the arithmetic equivalent."""
        if self.is_calendar_months:
            lo_idx = lo.year * 12 + lo.month
            hi_idx = hi.year * 12 + hi.month
            rows = []
            for i in range(lo_idx, hi_idx + 1):
                year, mon = (i - 1) // 12, (i - 1) % 12 + 1
                start = dt.date(year, mon, 1)
                end = dt.date(year, mon, monthrange(year, mon)[1])
                rows.append((start, end, i, f"{year}-{mon:02d}", f"{year}Q{(mon - 1) // 3 + 1}"))
            return pl.DataFrame(
                {
                    "start": [r[0] for r in rows],
                    "end": [r[1] for r in rows],
                    "period_idx": [r[2] for r in rows],
                    "period": [r[3] for r in rows],
                    "quarter": [r[4] for r in rows],
                },
                schema={
                    "start": pl.Date,
                    "end": pl.Date,
                    "period_idx": pl.Int64,
                    "period": pl.String,
                    "quarter": pl.String,
                },
            )
        return self._table_covering(lo, hi)

    def attach(
        self,
        df: pl.DataFrame,
        date_col: str,
        *,
        idx: str | None = None,
        period: str | None = None,
        quarter: str | None = None,
    ) -> pl.DataFrame:
        """Return df with the requested fiscal columns added (only the aliases
        asked for). calendar_months uses the engine's historical expressions,
        so default-calendar outputs stay byte-identical."""
        if self.is_calendar_months:
            exprs = []
            d = pl.col(date_col)
            if idx:
                exprs.append((d.dt.year() * 12 + d.dt.month()).alias(idx))
            if period:
                exprs.append(d.dt.strftime("%Y-%m").alias(period))
            if quarter:
                exprs.append(
                    (d.dt.year().cast(pl.String) + "Q" + d.dt.quarter().cast(pl.String)).alias(
                        quarter
                    )
                )
            return df.with_columns(exprs)
        dates = df.filter(pl.col(date_col).is_not_null())[date_col]
        if dates.len() == 0:
            cols = [
                pl.lit(None, dtype=pl.Int64).alias(idx) if idx else None,
                pl.lit(None, dtype=pl.String).alias(period) if period else None,
                pl.lit(None, dtype=pl.String).alias(quarter) if quarter else None,
            ]
            return df.with_columns([c for c in cols if c is not None])
        table = self._table_covering(dates.min(), dates.max())  # type: ignore[arg-type]
        sel = table.select(
            pl.col("start"),
            pl.col("end").alias("_fiscal_end"),
            *([pl.col("period_idx").alias(idx)] if idx else []),
            *([pl.col("period").alias(period)] if period else []),
            *([pl.col("quarter").alias(quarter)] if quarter else []),
        )
        out = df.with_row_index("_fiscal_row").sort(date_col, nulls_last=True)
        out = out.join_asof(
            sel,
            left_on=date_col,
            right_on="start",
            strategy="backward",
            check_sortedness=False,
        )
        outside = out.filter(
            pl.col(date_col).is_not_null()
            & (pl.col("_fiscal_end").is_null() | (pl.col(date_col) > pl.col("_fiscal_end")))
        )
        if outside.height:
            bad = outside[date_col].head(5).to_list()
            msg = (
                f"{outside.height} date(s) fall outside the configured fiscal "
                f"calendar's periods, e.g. {bad}. Fix the fiscal_periods table "
                f"or the calendar config; the engine never guesses a period."
            )
            raise FiscalCalendarError(msg)
        return out.sort("_fiscal_row").drop("_fiscal_row", "start", "_fiscal_end")


def build_calendar(cfg: FiscalConfig, fiscal_periods: pl.DataFrame | None = None) -> FiscalCalendar:
    """Construct the calendar from config (+ the mapped fiscal_periods table
    for kind=custom). Misconfiguration fails actionably."""
    notes: list[str] = []
    if cfg.calendar == "calendar_months":
        return FiscalCalendar(kind="calendar_months", config=cfg)
    if cfg.calendar in _PATTERNS:
        return FiscalCalendar(kind=cfg.calendar, config=cfg)
    # custom
    if fiscal_periods is None:
        msg = (
            "fiscal.calendar is 'custom' but no fiscal_periods reference table "
            "is mapped (columns: period_label, start_date, end_date, optional "
            "quarter_label). Map the table or pick a built-in calendar."
        )
        raise FiscalCalendarError(msg)
    required = {"period_label", "start_date", "end_date"}
    missing = required - set(fiscal_periods.columns)
    if missing:
        msg = f"fiscal_periods table is missing column(s) {sorted(missing)}"
        raise FiscalCalendarError(msg)
    t = fiscal_periods.sort("start_date")
    if t.filter(pl.col("end_date") < pl.col("start_date")).height:
        msg = "fiscal_periods has period(s) whose end_date precedes start_date"
        raise FiscalCalendarError(msg)
    overlap = t.filter(pl.col("start_date") <= pl.col("end_date").shift(1))
    if overlap.height:
        bad = overlap["period_label"].head(5).to_list()
        msg = f"fiscal_periods periods overlap, e.g. {bad}; periods must be disjoint"
        raise FiscalCalendarError(msg)
    if "quarter_label" in t.columns:
        quarter = pl.col("quarter_label")
    else:
        quarter = pl.col("period_label")
        notes.append(
            "fiscal_periods has no quarter_label: peer-group quarters fall "
            "back to the period itself"
        )
    table = t.select(
        pl.col("start_date").alias("start"),
        pl.col("end_date").alias("end"),
        pl.int_range(pl.len(), dtype=pl.Int64).alias("period_idx"),
        pl.col("period_label").alias("period"),
        quarter.alias("quarter"),
    )
    return FiscalCalendar(kind="custom", config=cfg, table=table, notes=tuple(notes))
