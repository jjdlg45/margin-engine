"""Generator configuration and scenario presets.

Everything the generator does is parameterized here. Presets modify only the
parameters relevant to the condition they create; everything else stays at
defaults so presets are comparable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Preset(StrEnum):
    WELL_IDENTIFIED = "well_identified"
    WEAK_INSTRUMENT = "weak_instrument"
    INVALID_INSTRUMENT = "invalid_instrument"
    SPARSE = "sparse"
    PRICE_CONSTANT = "price_constant"


class MessRates(BaseModel):
    """Rates at which each mess type is injected. All relative to clean sale lines."""

    model_config = ConfigDict(extra="forbid")

    return_rate: float = 0.04  # share of sale lines that get a linked return
    credit_memo_rate: float = 0.02  # share of invoices that get a credit memo
    missing_cost_rate: float = 0.01  # share of lines with cost blanked
    uom_alt_rate: float = 0.08  # share of lines expressed in an alternate UOM
    verified_dup_rate: float = 0.005  # exact re-export: same source line key
    lookalike_dup_rate: float = 0.003  # identical values, different source key
    price_outlier_rate: float = 0.002  # data-entry price errors (x10 / /10)
    commercial_outlier_rate: float = 0.002  # genuine but extreme discounts
    zero_qty_rate: float = 0.001  # malformed zero-quantity rows
    stockout_rate: float = 0.02  # item-month unavailability


class DemandParams(BaseModel):
    """Structural demand / pricing parameters."""

    model_config = ConfigDict(extra="forbid")

    # Elasticity range segments are drawn from (uniform).
    elasticity_min: float = -2.5
    elasticity_max: float = -0.8
    # Segment base price index range (realized price / list).
    base_index_min: float = 0.80
    base_index_max: float = 0.98
    # Std dev of the time-varying unobserved demand shock (customer x month).
    demand_shock_sd: float = 0.30
    # AR(1) persistence of instrument processes.
    instrument_ar: float = 0.70
    # Innovation sd of supplier cost shock and category tariff shock (log scale).
    cost_shock_sd: float = 0.05
    tariff_shock_sd: float = 0.04
    # Pass-through of instrument shocks into realized price (first-stage strength).
    price_passthrough: float = 0.60
    # Endogenous rep discounting: d(log price)/d(demand shock). Positive means
    # reps cut price when demand is weak -> naive OLS biased TOWARD ZERO.
    endogeneity_gamma: float = 0.08
    # Direct effect of the tariff instrument on demand (0 = valid instrument).
    tariff_demand_leak: float = 0.0
    # Idiosyncratic price noise (log sd).
    price_noise_sd: float = 0.03
    # Pair-level price heterogeneity within segment (log sd).
    pair_price_sd: float = 0.06
    # Log mean / sd of pair base monthly quantity.
    base_qty_log_mean: float = 2.2
    base_qty_log_sd: float = 0.8
    # Monthly purchase incidence probability for an active, available pair.
    purchase_prob: float = 0.60


class GeneratorConfig(BaseModel):
    """Full generator parameterization. Seeded and deterministic."""

    model_config = ConfigDict(extra="forbid")

    seed: int = 42
    preset: Preset = Preset.WELL_IDENTIFIED

    n_customers: int = Field(default=200, ge=10)
    n_items: int = Field(default=600, ge=20)
    n_reps: int = 12
    n_suppliers: int = 12
    n_categories: int = 8
    n_families: int = 20
    months: int = 24
    start_year: int = 2024
    start_month: int = 1

    # Items per customer assortment (uniform range).
    assortment_min: int = 10
    assortment_max: int = 40
    # Share of items that are long-tail (rarely bought).
    long_tail_share: float = 0.30
    # Share of items that are zero-cost service items.
    service_item_share: float = 0.02

    # List price revisions per item over the horizon (uniform int range).
    list_revisions_min: int = 1
    list_revisions_max: int = 3

    # Customer rebate programs: share of customers on a line-accrued program
    # (exact per-line amounts in export) vs. a settlement program (customer x
    # month totals the engine must allocate).
    line_rebate_share: float = 0.30
    settlement_rebate_share: float = 0.25
    rebate_rate_min: float = 0.01
    rebate_rate_max: float = 0.05

    # Freight: share of orders with billed freight; concession share; expense always.
    freight_billed_share: float = 0.40
    freight_concession_share: float = 0.25

    # Vendor rebates: share of suppliers with a program, rate on purchases.
    vendor_rebate_supplier_share: float = 0.40
    vendor_rebate_rate: float = 0.02

    # Contracts: share of customers with contract pricing on part of assortment.
    contract_customer_share: float = 0.25

    mess: MessRates = MessRates()
    demand: DemandParams = DemandParams()

    @classmethod
    def for_preset(cls, preset: Preset, seed: int = 42, **overrides: object) -> GeneratorConfig:
        """Build a config for a named scenario preset."""
        base: dict[str, object] = {"seed": seed, "preset": preset}
        demand = DemandParams()
        if preset is Preset.WEAK_INSTRUMENT:
            demand = demand.model_copy(update={"price_passthrough": 0.03})
        elif preset is Preset.INVALID_INSTRUMENT:
            # Tariff shock leaks directly into demand: exclusion violated for
            # one instrument; the overidentification test should reject.
            demand = demand.model_copy(update={"tariff_demand_leak": 1.5})
        elif preset is Preset.PRICE_CONSTANT:
            demand = demand.model_copy(
                update={
                    "price_passthrough": 0.0,
                    "endogeneity_gamma": 0.0,
                    "price_noise_sd": 0.0,
                    "cost_shock_sd": 0.0,
                    "tariff_shock_sd": 0.0,
                }
            )
            # No list revisions either: prices are truly constant within pair.
            base["list_revisions_min"] = 0
            base["list_revisions_max"] = 0
        base["demand"] = demand
        if preset is Preset.SPARSE:
            base["n_customers"] = 60
            base["assortment_min"] = 4
            base["assortment_max"] = 10
            demand = demand.model_copy(update={"purchase_prob": 0.15})
            base["demand"] = demand
        base.update(overrides)
        return cls.model_validate(base)
