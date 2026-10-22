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
    # Harder, informational-only world: Poisson count quantities (E[log Q] is
    # then nonlinear in log price, so the planted beta is only approximately
    # the log-log estimand) and the noise levels from before the default world
    # was tightened. Recovery on this preset is REPORTED, not gated.
    REALISTIC_NOISE = "realistic_noise"
    # Run 4 (24.5): customer demand shocks follow an AR(1) over months, so
    # residuals are correlated within customers across time. Used by the
    # persistent-demand coverage study that picks the clustering default.
    PERSISTENT_DEMAND = "persistent_demand"
    # Run 4 (24.9) misspecified worlds — informational, never gated:
    # pair elasticities vary around the segment value (the engine's
    # one-beta-per-segment model is wrong by construction);
    HETEROGENEOUS_ELASTICITY = "heterogeneous_elasticity"
    # demand curvature makes elasticity depend on the price level (the
    # constant-elasticity log-log model is wrong by construction);
    NONCONSTANT_ELASTICITY = "nonconstant_elasticity"
    # competitor prices co-move with the cost instruments and shift demand:
    # the exclusion restriction is violated for BOTH instruments in a way
    # overidentification cannot detect (both move together).
    COMPETITOR_COMOVEMENT = "competitor_comovement"


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
    # Segment base price index range (realized price / list). The range is
    # wide enough that adjacent planted segments differ by more than the
    # within-segment pair heterogeneity — segments that overlap beyond
    # recoverability would make every validation vacuous.
    base_index_min: float = 0.75
    base_index_max: float = 0.98
    # Std dev of the time-varying unobserved demand shock (customer x month).
    demand_shock_sd: float = 0.22
    # AR(1) persistence of the customer demand shock (24.5). 0 = iid (default,
    # unchanged worlds); the persistent_demand preset raises it.
    demand_ar: float = 0.0
    # Pair-level elasticity heterogeneity WITHIN segment (24.9, misspecified
    # world): sd of a normal around the segment beta. 0 = exact segment betas.
    within_segment_beta_sd: float = 0.0
    # Demand curvature (24.9): log mu += curvature x (log P/P_ref)^2, so the
    # local elasticity beta + 2 x curvature x log(P/P_ref) is price-dependent.
    elasticity_curvature: float = 0.0
    # Competitor co-movement (24.9/24.10): a category market price index moves
    # with the industry cost shocks (same pass-through as own prices) and
    # shifts demand by this factor. 0 = off.
    competitor_comovement: float = 0.0
    # AR(1) persistence of instrument processes.
    instrument_ar: float = 0.70
    # Innovation sd of supplier cost shock and category tariff shock (log scale).
    cost_shock_sd: float = 0.05
    tariff_shock_sd: float = 0.04
    # Pass-through of instrument shocks into realized price (first-stage strength).
    price_passthrough: float = 0.75
    # Endogenous rep discounting: d(log price)/d(demand shock). Positive means
    # reps cut price when demand is weak -> naive OLS biased TOWARD ZERO.
    endogeneity_gamma: float = 0.08
    # Direct effect of the tariff instrument on demand (0 = valid instrument).
    tariff_demand_leak: float = 0.0
    # Idiosyncratic price noise (log sd).
    price_noise_sd: float = 0.03
    # Pair-level price heterogeneity within segment (log sd).
    pair_price_sd: float = 0.05
    # Log mean / sd of pair base monthly quantity.
    base_qty_log_mean: float = 3.0
    base_qty_log_sd: float = 0.7
    # Multiplicative quantity noise (log sd). Quantity is exactly log-linear:
    # Q = max(1, round(exp(log mu + eps))), so the planted elasticity IS the
    # estimand of log-log and PPML models (a Poisson draw would make E[log Q]
    # nonlinear in log price and the planted beta unrecoverable in principle).
    qty_noise_sd: float = 0.20
    # Quantity process: "loglinear" (default, exactly recoverable estimand) or
    # "poisson" (count draws; used by the realistic_noise preset).
    qty_process: str = "loglinear"
    # Monthly purchase incidence probability for an active, available pair.
    purchase_prob: float = 0.60
    # Probability a customer also carries any given KVI item (Phase 15).
    kvi_carriage_prob: float = 0.5


class GeneratorConfig(BaseModel):
    """Full generator parameterization. Seeded and deterministic."""

    model_config = ConfigDict(extra="forbid")

    seed: int = 42
    preset: Preset = Preset.WELL_IDENTIFIED

    # Default world size: large enough that every planted segment clears the
    # engine's distinct-customer leaf minimums (the smallest segment is
    # ~7.5% of customers; 400 x 7.5% = 30 >= 20).
    n_customers: int = Field(default=400, ge=10)
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

    # Multi-currency (Phase 9): share of customers invoiced per currency.
    # Default is single-currency so Run-1 worlds are unchanged. Foreign lines
    # express revenue amounts in the customer's currency, converted from the
    # internal USD world at the planted monthly rate; list prices and costs
    # stay in the reporting currency (USD), as for a US distributor invoicing
    # abroad at the month's rate.
    currency_shares: dict[str, float] = {"USD": 1.0}
    # Planted base rates, USD per unit of local currency.
    fx_base: dict[str, float] = {"EUR": 1.08, "GBP": 1.26, "CAD": 0.73}
    # Monthly random-walk sd of the planted fx rate (relative).
    fx_walk_sd: float = 0.01

    # Substitution groups (Phase 17): pairs of same-family items with planted
    # positive cross-price elasticity. Default off: cross effects between items
    # sharing suppliers/categories would contaminate the own-price IV worlds
    # of Run 1; Phase 17 worlds enable them explicitly.
    n_substitution_groups: int = 0
    cross_elasticity_min: float = 0.3
    cross_elasticity_max: float = 0.8

    # Hybrid demonstration world (Run 4, 29.4): when set, supplier cost paths
    # come from REAL external series (e.g. BLS PPI, demeaned log levels) in
    # place of the simulated AR(1) cost shocks. Suppliers are assigned series
    # round-robin; each path must cover at least `months` entries. Everything
    # else (demand, endogeneity, tariff instrument) stays simulated, so the
    # planted elasticity remains the known truth to recover.
    external_cost_paths: list[list[float]] | None = None

    mess: MessRates = MessRates()
    demand: DemandParams = DemandParams()

    @classmethod
    def for_preset(cls, preset: Preset, seed: int = 42, **overrides: object) -> GeneratorConfig:
        """Build a config for a named scenario preset."""
        base: dict[str, object] = {"seed": seed, "preset": preset}
        demand = DemandParams()
        if preset is Preset.WEAK_INSTRUMENT:
            demand = demand.model_copy(update={"price_passthrough": 0.005})
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
        elif preset is Preset.REALISTIC_NOISE:
            demand = demand.model_copy(
                update={
                    "qty_process": "poisson",
                    "demand_shock_sd": 0.30,
                    "price_passthrough": 0.60,
                    "pair_price_sd": 0.06,
                }
            )
        elif preset is Preset.PERSISTENT_DEMAND:
            demand = demand.model_copy(update={"demand_ar": 0.70})
        elif preset is Preset.HETEROGENEOUS_ELASTICITY:
            demand = demand.model_copy(update={"within_segment_beta_sd": 0.40})
        elif preset is Preset.NONCONSTANT_ELASTICITY:
            demand = demand.model_copy(update={"elasticity_curvature": -1.5})
        elif preset is Preset.COMPETITOR_COMOVEMENT:
            demand = demand.model_copy(update={"competitor_comovement": 0.8})
        base["demand"] = demand
        if preset is Preset.SPARSE:
            base["n_customers"] = 40
            base["assortment_min"] = 4
            base["assortment_max"] = 8
            demand = demand.model_copy(update={"purchase_prob": 0.07, "kvi_carriage_prob": 0.0})
            base["demand"] = demand
        base.update(overrides)
        return cls.model_validate(base)
