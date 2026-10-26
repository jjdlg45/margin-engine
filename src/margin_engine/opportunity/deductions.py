"""Deductions with explicit denominators (Run 6, Phase 66 / Finding 8).

Every post-invoice adjustment the engine nets out of pocket revenue has a
FORM, a BASE (denominator) and a SOURCE:

- percent_of_net (flat program rates and realized freight-concession rates):
  rates on the SAME base (net invoiced value) that apply independently are
  summed into d; rates on different bases are never summed.
- amount_per_unit (fixed deductions F): cents per normalized unit; F cancels
  out of price gaps but not of levels.
- tiered_cumulative (retrospective tier programs): the rebate is
  rate(cumulative basis) x cumulative basis, so a candidate price RE-RATES the
  customer's whole basis when it crosses a tier. These are RECOMPUTED at the
  candidate revenue, never flattened into d; where the tier table is missing,
  the (1 - d) result is labeled a fixed-rate approximation instead.

The treatment each pair actually received is labeled on every opportunity
record (`deduction_treatment`): flat_rate_exact, recomputed_tiered, or
fixed_rate_approximation. The per-customer coverage table reports how much of
d is observed (realized history), contract-derived (program rates) or
missing, so a reader can see how much of the deduction picture the data
actually supports.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

TREATMENT_FLAT = "flat_rate_exact"
TREATMENT_RECOMPUTED = "recomputed_tiered"
TREATMENT_APPROX = "fixed_rate_approximation"


@dataclass
class TieredDeductionContext:
    """What the recomputation needs: each tiered customer-program's baseline
    eligible basis (the current year's cumulative net) and its tier table."""

    # customer_id, rebate_program_id, basis_cents (current-year eligible basis)
    bases: pl.DataFrame
    # rebate_program_id, min_amount_cents, rate_bp (sorted ascending)
    tiers: pl.DataFrame

    def rate_bp(self, program: str, basis_cents: float) -> int:
        t = self.tiers.filter(
            (pl.col("rebate_program_id") == program) & (pl.col("min_amount_cents") <= basis_cents)
        ).sort("min_amount_cents")
        return int(t["rate_bp"][-1]) if t.height else 0

    def rebate_cents(self, program: str, basis_cents: float) -> float:
        """Retrospective definition: the final tier rate re-rates the WHOLE
        basis - the same economics the settlement truth uses."""
        if basis_cents <= 0:
            return 0.0  # negative basis accrues nothing (fail closed)
        return basis_cents * self.rate_bp(program, basis_cents) / 10_000

    def delta_cents(self, customer: str, delta_revenue_cents: float) -> float | None:
        """Rebate change when the customer's eligible basis moves by
        delta_revenue: sum over the customer's tiered programs of
        rebate(B0 + dR) - rebate(B0). None when the customer holds no tiered
        program (nothing to recompute)."""
        rows = self.bases.filter(pl.col("customer_id") == customer)
        if rows.height == 0:
            return None
        total = 0.0
        for r in rows.to_dicts():
            b0 = float(r["basis_cents"])
            program = str(r["rebate_program_id"])
            total += self.rebate_cents(program, b0 + delta_revenue_cents) - self.rebate_cents(
                program, b0
            )
        return total


def build_tiered_context(
    rebate_programs: pl.DataFrame | None,
    rebate_program_tiers: pl.DataFrame | None,
    tiered_summary: pl.DataFrame | None,
) -> TieredDeductionContext | None:
    """Context from the engine's own frames: the LATEST year's eligible basis
    per customer-program (the year a candidate price would land in)."""
    if rebate_programs is None or rebate_program_tiers is None or tiered_summary is None:
        return None
    if tiered_summary.height == 0:
        return None
    latest = (
        tiered_summary.sort("year")
        .group_by("customer_id", "rebate_program_id", maintain_order=True)
        .agg(pl.col("eligible_basis_cents").last().alias("basis_cents"))
    )
    tiers = rebate_program_tiers.select(
        "rebate_program_id",
        "min_amount_cents",
        (pl.col("rate_pct") * 100).round(0).cast(pl.Int64).alias("rate_bp"),
    ).sort("rebate_program_id", "min_amount_cents")
    if latest.height == 0 or tiers.height == 0:
        return None
    return TieredDeductionContext(
        bases=latest.sort("customer_id", "rebate_program_id"), tiers=tiers
    )


def deduction_coverage(
    guidance: pl.DataFrame, tiered_customers: set[str] | None = None
) -> pl.DataFrame:
    """Per customer: how much of the percentage deduction rate d is OBSERVED
    (realized concession history), CONTRACT-DERIVED (program rates), or
    carried by a tiered formula that is recomputed instead of flattened.
    Shares of the known total; a customer with no known components reports
    zero coverage, never an invented rate."""
    cols = {"customer_id", "rebate_bp", "concession_bp"}
    if not cols.issubset(guidance.columns):
        return pl.DataFrame(
            schema={
                "customer_id": pl.String,
                "d_total_bp": pl.Int64,
                "observed_bp": pl.Int64,
                "contract_derived_bp": pl.Int64,
                "tiered_recomputed": pl.Boolean,
                "observed_share": pl.Float64,
                "contract_share": pl.Float64,
            }
        )
    tiered = tiered_customers or set()
    out = (
        guidance.group_by("customer_id")
        .agg(
            pl.col("rebate_bp").first().alias("contract_derived_bp"),
            pl.col("concession_bp").first().alias("observed_bp"),
        )
        .with_columns(
            (pl.col("contract_derived_bp") + pl.col("observed_bp")).alias("d_total_bp"),
            pl.col("customer_id").cast(pl.String).is_in(sorted(tiered)).alias("tiered_recomputed"),
        )
        .with_columns(
            pl.when(pl.col("d_total_bp") > 0)
            .then(pl.col("observed_bp") / pl.col("d_total_bp"))
            .otherwise(0.0)
            .alias("observed_share"),
            pl.when(pl.col("d_total_bp") > 0)
            .then(pl.col("contract_derived_bp") / pl.col("d_total_bp"))
            .otherwise(0.0)
            .alias("contract_share"),
        )
        .sort("customer_id")
    )
    return out.select(
        pl.col("customer_id").cast(pl.String),
        "d_total_bp",
        "observed_bp",
        "contract_derived_bp",
        "tiered_recomputed",
        "observed_share",
        "contract_share",
    )
