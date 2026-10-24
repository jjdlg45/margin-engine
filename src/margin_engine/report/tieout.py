"""Books tie-out (Run 4, 27.3): compare engine monthly totals to the general
ledger. Optional input `gl_totals` (month, net_sales, cogs, optionally
returns). A month FAILS when the difference exceeds both the configured
percentage and cents tolerances; failing months are flagged, never hidden.

Measure definitions (management view, stated so the comparison is honest):
- net_sales_engine  = sum of net_amount_cents over sale AND return lines per
  invoice month [cents]. Returns are recognized when they occur (see the GAAP
  mapping in docs/methodology.md), so a GL that books a refund liability at
  sale will differ in months with heavy returns - that is a finding, not an
  error to suppress.
- cogs_engine       = sum of product_cost_cents over sale and return lines
  with KNOWN cost [cents]. The coverage share is reported next to the
  comparison: a GL books full COGS, so low cost coverage explains gaps.
- returns_engine    = sum of net_amount_cents over return lines (negative).
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from margin_engine.config import Rules
from margin_engine.fiscal import FiscalCalendar, build_calendar


@dataclass
class TieoutResult:
    table: pl.DataFrame  # month, measure, engine_cents, gl_cents, diffs, flag
    n_failing: int
    cost_coverage_share: float
    notes: list[str]


def _monthly(ledger: pl.DataFrame, fiscal: FiscalCalendar) -> pl.DataFrame:
    """Engine totals per fiscal period (Phase 36). gl_totals' month labels must
    use the same calendar: %Y-%m under the default, the configured period
    labels otherwise."""
    sale_ret = ledger.filter(pl.col("transaction_type").is_in(["sale", "return"]))
    return (
        fiscal.attach(sale_ret, "invoice_date", period="month")
        .group_by("month")
        .agg(
            pl.col("net_amount_cents").sum().alias("net_sales_engine"),
            pl.col("product_cost_cents").drop_nulls().sum().alias("cogs_engine"),
            pl.when(pl.col("transaction_type") == "return")
            .then(pl.col("net_amount_cents"))
            .otherwise(0)
            .sum()
            .alias("returns_engine"),
        )
        .sort("month")
    )


def run_tieout(
    ledger: pl.DataFrame,
    gl_totals: pl.DataFrame,
    rules: Rules,
    fiscal: FiscalCalendar | None = None,
) -> TieoutResult:
    """gl_totals: month (period label in the configured fiscal calendar;
    YYYY-MM under the default), net_sales_cents, cogs_cents, optionally
    returns_cents - canonical columns from the mapping loader."""
    if fiscal is None:
        fiscal = build_calendar(rules.fiscal)
    tol_pct = rules.reporting.tieout_tolerance_pct
    tol_cents = rules.reporting.tieout_tolerance_cents
    notes: list[str] = []

    covered = ledger.filter(
        pl.col("transaction_type").is_in(["sale", "return"]) & (pl.col("net_amount_cents") != 0)
    )
    with_cost = covered.filter(pl.col("product_cost_cents").is_not_null())
    cov = (
        int(with_cost["net_amount_cents"].abs().sum() or 0)
        / int(covered["net_amount_cents"].abs().sum() or 1)
        if covered.height
        else 0.0
    )
    if cov < 0.999:
        notes.append(
            f"engine COGS covers lines with known cost only ({cov:.1%} of sale/return "
            "revenue); a GL books full COGS, so part of any COGS gap is coverage"
        )

    engine = _monthly(ledger, fiscal)
    measures = [("net_sales", "net_sales_engine", "net_sales_cents")]
    if "cogs_cents" in gl_totals.columns:
        measures.append(("cogs", "cogs_engine", "cogs_cents"))
    if "returns_cents" in gl_totals.columns:
        measures.append(("returns", "returns_engine", "returns_cents"))

    rows = []
    joined = gl_totals.join(engine, on="month", how="full", coalesce=True).sort("month")
    for r in joined.to_dicts():
        for name, eng_col, gl_col in measures:
            eng = r.get(eng_col)
            gl = r.get(gl_col)
            if gl is None:
                continue  # GL does not carry this measure for the month
            eng_v = int(eng) if eng is not None else 0
            gl_v = int(gl)
            diff = eng_v - gl_v
            diff_pct = (diff / gl_v * 100) if gl_v != 0 else (0.0 if diff == 0 else float("inf"))
            within = abs(diff) <= tol_cents or abs(diff_pct) <= tol_pct
            rows.append(
                {
                    "month": r["month"],
                    "measure": name,
                    "engine_cents": eng_v,
                    "gl_cents": gl_v,
                    "diff_cents": diff,
                    "diff_pct": round(diff_pct, 4) if diff_pct != float("inf") else None,
                    "within_tolerance": within,
                }
            )
    months_in_gl = set(gl_totals["month"].to_list())
    extra_engine = [m for m in engine["month"].to_list() if m not in months_in_gl]
    if extra_engine:
        notes.append(
            f"engine has activity in {len(extra_engine)} month(s) absent from the GL "
            f"file ({', '.join(extra_engine[:6])}) - not compared, not hidden"
        )
    table = pl.DataFrame(rows)
    n_fail = int((~table["within_tolerance"]).sum()) if table.height else 0
    return TieoutResult(table=table, n_failing=n_fail, cost_coverage_share=float(cov), notes=notes)
