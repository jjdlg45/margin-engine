"""Monthly GL variance check (Run 4 'books tie-out', reframed in Run 6 Phase
64 as a CONTROL, not a proof): compare engine monthly totals to the general
ledger. Optional input `gl_totals` (month, net_sales, cogs, optionally
returns). Net sales and COGS reconcile separately; every month-measure shows
the signed and absolute variance, the variance percentage (guarded against
zero, negative and tiny denominators), the tolerance used, and a status.
Variances outside tolerance are flagged, never hidden.

Statuses: `gl_exact` (every compared month-measure matches to the cent),
`gl_within_tolerance`, `gl_variances_outside_tolerance`; when no GL file is
supplied the run-level status is `gl_not_supplied` (reported where the check
would appear - absence is stated, not implied). Source-total reconciliation
(`source totals reconciled`) is the separate, always-on exact check in the
reconciliation report; the GL check is a variance control on top of it.

Measure definitions (management view, stated so the comparison is honest):
- net_sales_engine  = sum of net_amount_cents over sale AND return lines per
  period [cents]. Returns are recognized when they occur (see the GAAP
  mapping in docs/methodology.md), so a GL that books a refund liability at
  sale will differ in months with heavy returns - that is a finding, not an
  error to suppress.
- cogs_engine       = sum of product_cost_cents over sale and return lines
  with KNOWN cost [cents]. The coverage share is reported next to the
  comparison: a GL books full COGS, so low cost coverage explains gaps.
- returns_engine    = sum of net_amount_cents over return lines (negative).

Trace evidence: a deterministic sample of line -> invoice -> period rows with
the period's engine and GL totals beside them, so a reviewer can follow one
dollar from the source document into the monthly bridge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from margin_engine.config import Rules
from margin_engine.fiscal import FiscalCalendar, build_calendar

STATUS_EXACT = "gl_exact"
STATUS_WITHIN = "gl_within_tolerance"
STATUS_OUTSIDE = "gl_variances_outside_tolerance"
STATUS_NOT_SUPPLIED = "gl_not_supplied"

ACCOUNT_MAPPING = {
    "net_sales": (
        "engine: net amounts over sale and return lines per period (returns "
        "recognized when they occur); GL: the net sales account total"
    ),
    "cogs": (
        "engine: product cost over sale and return lines with known cost "
        "(coverage share stated); GL: the cost of goods sold account total"
    ),
    "returns": "engine: net amounts over return lines (negative); GL: the returns account total",
}


@dataclass
class TieoutResult:
    table: pl.DataFrame  # month, measure, totals, variances, tolerance, status
    n_failing: int
    cost_coverage_share: float
    status: str
    fiscal_mapping: str
    account_mapping: dict[str, str] = field(default_factory=dict)
    unresolved_items: list[str] = field(default_factory=list)
    trace_sample: pl.DataFrame | None = None
    notes: list[str] = field(default_factory=list)


def _monthly(ledger: pl.DataFrame, fiscal: FiscalCalendar) -> pl.DataFrame:
    """Engine totals per fiscal period (Phase 36). gl_totals' month labels must
    use the same calendar: %Y-%m under the default, the configured period
    labels otherwise."""
    sale_ret = ledger.filter(pl.col("transaction_type").is_in(["sale", "return"]))
    # GL net sales never include collected taxes: compare the tax-free product
    # amount when the tax stage ran (Run 6, Phase 63/64); older frames without
    # it fall back to net_amount.
    net_col = "net_product_cents" if "net_product_cents" in ledger.columns else "net_amount_cents"
    return (
        fiscal.attach(sale_ret, "invoice_date", period="month")
        .group_by("month")
        .agg(
            pl.col(net_col).sum().alias("net_sales_engine"),
            pl.col("product_cost_cents").drop_nulls().sum().alias("cogs_engine"),
            pl.when(pl.col("transaction_type") == "return")
            .then(pl.col("net_amount_cents"))
            .otherwise(0)
            .sum()
            .alias("returns_engine"),
        )
        .sort("month")
    )


def _trace_sample(
    ledger: pl.DataFrame,
    fiscal: FiscalCalendar,
    engine: pl.DataFrame,
    gl_totals: pl.DataFrame,
    *,
    per_month: int = 2,
    max_months: int = 4,
) -> pl.DataFrame:
    """Deterministic line -> invoice -> period trace rows for months the GL
    carries, with the period's engine and GL net-sales totals beside each
    line, so one dollar can be followed from source document to the bridge."""
    if not {"source_line_key", "invoice_id"}.issubset(ledger.columns):
        return pl.DataFrame()
    months = sorted(set(gl_totals["month"].to_list()) & set(engine["month"].to_list()))[:max_months]
    if not months:
        return pl.DataFrame()
    lines = (
        fiscal.attach(
            ledger.filter(pl.col("transaction_type").is_in(["sale", "return"])),
            "invoice_date",
            period="month",
        )
        .filter(pl.col("month").is_in(months))
        .sort("month", "source_line_key")
        .group_by("month", maintain_order=True)
        .head(per_month)
        .select(
            "month",
            pl.col("source_line_key").cast(pl.String),
            pl.col("invoice_id").cast(pl.String),
            pl.col("transaction_type").cast(pl.String),
            "net_amount_cents",
            "product_cost_cents",
        )
    )
    return (
        lines.join(
            engine.select("month", "net_sales_engine"),
            on="month",
            how="left",
        )
        .join(
            gl_totals.select("month", pl.col("net_sales_cents").alias("net_sales_gl")),
            on="month",
            how="left",
        )
        .rename({"net_sales_engine": "period_engine_net_sales_cents"})
        .rename({"net_sales_gl": "period_gl_net_sales_cents"})
        .sort("month", "source_line_key")
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
    fx_converted = any(c.endswith("_src") for c in ledger.columns)
    tol_pct = rules.reporting.tieout_tolerance_pct
    tol_cents = rules.reporting.tieout_tolerance_cents
    notes: list[str] = []
    if fx_converted:
        # Analytical vs book translation (Run 6, Phase 85). The engine
        # converts each line at the rate of its invoice period - the
        # analytical view, where a June sale is a June sale at June's rate. A
        # general ledger translated at closing or average rates values the
        # same transactions differently, and the gap is translation policy,
        # not a missing dollar. Said here so a multi-currency variance is read
        # against the right explanation first.
        notes.append(
            "multi-currency: engine totals convert each line at its invoice "
            "period's rate (analytical view, fx_rate_policy invoice_date). A GL "
            "translated at closing or average rates will differ by translation "
            "policy alone - a standing reconciling item on every comparison "
            "below, not a missing amount"
        )
    unresolved: list[str] = []

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
        unresolved.append(
            f"cost known on {cov:.1%} of sale/return revenue: the uncovered share "
            "is a standing reconciling item on every COGS comparison"
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
            variance = eng_v - gl_v
            # Percentage guard (Phase 64): the denominator is the ABSOLUTE GL
            # amount, and a zero or tiny denominator (below the cents
            # tolerance) yields no percentage - the cents tolerance judges
            # those months, so a near-zero GL month can never divide its way
            # into a scary or soothing percent.
            denom = abs(gl_v)
            variance_pct = round(variance / denom * 100, 4) if denom > tol_cents else None
            within = abs(variance) <= tol_cents or (
                variance_pct is not None and abs(variance_pct) <= tol_pct
            )
            row_status = (
                STATUS_EXACT if variance == 0 else (STATUS_WITHIN if within else STATUS_OUTSIDE)
            )
            rows.append(
                {
                    "month": r["month"],
                    "measure": name,
                    "engine_cents": eng_v,
                    "gl_cents": gl_v,
                    "variance_cents": variance,
                    "variance_abs_cents": abs(variance),
                    "variance_pct": variance_pct,
                    "tolerance_pct": tol_pct,
                    "tolerance_cents": tol_cents,
                    "status": row_status,
                    "within_tolerance": within,
                }
            )
    months_in_gl = set(gl_totals["month"].to_list())
    extra_engine = [m for m in engine["month"].to_list() if m not in months_in_gl]
    if extra_engine:
        item = (
            f"engine has activity in {len(extra_engine)} period(s) absent from the GL "
            f"file ({', '.join(extra_engine[:6])}) - not compared, not hidden"
        )
        notes.append(item)
        unresolved.append(item)
    extra_gl = [m for m in gl_totals["month"].to_list() if m not in set(engine["month"])]
    if extra_gl:
        item = (
            f"GL carries {len(extra_gl)} period(s) with no engine activity "
            f"({', '.join(map(str, extra_gl[:6]))}) - compared against zero"
        )
        notes.append(item)
        unresolved.append(item)

    table = pl.DataFrame(rows)
    n_fail = int((~table["within_tolerance"]).sum()) if table.height else 0
    if table.height == 0:
        status = STATUS_NOT_SUPPLIED
    elif n_fail:
        status = STATUS_OUTSIDE
    elif int((table["variance_cents"] != 0).sum()) == 0:
        status = STATUS_EXACT
    else:
        status = STATUS_WITHIN
    fiscal_mapping = (
        "calendar months (period labels YYYY-MM)"
        if fiscal.is_calendar_months
        else f"fiscal calendar '{fiscal.kind}' (GL month labels must use its period labels)"
    )
    trace = _trace_sample(ledger, fiscal, engine, gl_totals)
    return TieoutResult(
        table=table,
        n_failing=n_fail,
        cost_coverage_share=float(cov),
        status=status,
        fiscal_mapping=fiscal_mapping,
        account_mapping={k: v for k, v in ACCOUNT_MAPPING.items() if k in {m[0] for m in measures}},
        unresolved_items=unresolved,
        trace_sample=trace if trace.height else None,
        notes=notes,
    )
