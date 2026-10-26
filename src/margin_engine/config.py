"""Pydantic models for rules.yaml — all business policy, thresholds and model choices.

Config controls policy; code controls algorithms. Every section here maps to a
top-level key in rules.yaml and carries the defaults from the build spec
(section 8). Any real deployment overrides these in YAML, never in code.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base model: unknown keys are errors so typos in YAML fail loudly."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class SchemaConfig(StrictModel):
    """Config-file versioning so old configs are detected, not misread."""

    version: int = 1


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


class MappingConfig(StrictModel):
    """Which mapping YAML to use and how strictly to enforce semantics."""

    mapping_file: str | None = None
    # Fail closed: a calculation that needs a semantic the mapping does not
    # declare raises an actionable error instead of guessing.
    fail_on_missing_semantics: bool = True


# ---------------------------------------------------------------------------
# units_and_currency
# ---------------------------------------------------------------------------


class UnitsCurrencyConfig(StrictModel):
    reporting_currency: str = "USD"
    currency_decimals: int = Field(default=2, ge=0, le=6)
    # Run 2 (Phase 9): conversion policy. Default documented: rate as of invoice date.
    fx_rate_policy: Literal["invoice_date", "month_average", "fixed"] = "invoice_date"
    canonical_uom_policy: Literal["base_selling_unit"] = "base_selling_unit"


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------


class TransactionsConfig(StrictModel):
    # Duplicates removed only on verified identity (stable source key).
    dedupe_on: Literal["source_key", "verified_identity"] = "source_key"
    flag_lookalike_duplicates: bool = True
    # Credit-memo reasons that reduce realized revenue (price-related).
    price_credit_reasons: tuple[str, ...] = ("price_correction", "customer_rebate")
    # Reasons routed to cost categories instead of revenue.
    cost_credit_reasons: tuple[str, ...] = ("damage", "shortage", "quality", "warranty")
    # Reasons whose counterparty is the SUPPLIER (Run 6, Phase 61): recoveries
    # for supplier-caused damage and similar claims. They never touch customer
    # revenue; their dollars land in cost-to-serve with the supplier named as
    # the counterparty in the economic-event taxonomy.
    supplier_claim_reasons: tuple[str, ...] = ()
    # Economic-event classification of billed order charges by charge code
    # (Run 6, Phase 61), e.g. {"FRT": "freight_pass_through"}. Codes not listed
    # stay `other_adjustment` with a stated note - the engine never guesses
    # what a charge code means.
    charge_event_types: dict[str, str] = Field(default_factory=dict)
    zero_quantity_treatment: Literal["exclude_and_flag"] = "exclude_and_flag"


# ---------------------------------------------------------------------------
# hierarchy
# ---------------------------------------------------------------------------


class HierarchyConfig(StrictModel):
    """Customer analysis level (Phase 10): ship-to (line grain), bill-to, or
    parent account. Analytics group by the chosen level; the ledger always
    stays at ship-to grain with the original ids kept for audit."""

    analysis_level: Literal["ship_to", "bill_to", "parent"] = "ship_to"


# ---------------------------------------------------------------------------
# returns
# ---------------------------------------------------------------------------


class ReturnReasonPolicy(StrictModel):
    """Commercial policy for one return reason."""

    restocking_fee_pct: Decimal = Decimal("0")
    return_freight_paid_by: Literal["customer", "distributor"] = "distributor"


class ReturnsConfig(StrictModel):
    """Return economics policy (phase-8.2).

    When the export carries actual restocking fees / billed return freight,
    those are used (status `actual`). When absent, the reason policy and the
    default handling costs produce estimates labeled `estimated`.
    """

    reason_policies: dict[str, ReturnReasonPolicy] = Field(
        default_factory=lambda: {
            "customer_error": ReturnReasonPolicy(
                restocking_fee_pct=Decimal("0.15"), return_freight_paid_by="customer"
            ),
            "distributor_error": ReturnReasonPolicy(
                restocking_fee_pct=Decimal("0"), return_freight_paid_by="distributor"
            ),
            "defective": ReturnReasonPolicy(
                restocking_fee_pct=Decimal("0"), return_freight_paid_by="distributor"
            ),
        }
    )
    # Return disposition -> whether product cost is recovered (Run 6, Phase
    # 63). Resaleable and exchange returns put the product back (recovered,
    # the historical behavior and the default for unknown or absent
    # dispositions - stated in the run notes); damaged, obsolete and
    # not-received returns recover nothing, so the loss stays in cost of
    # goods sold.
    disposition_cost_recovery: dict[str, bool] = Field(
        default_factory=lambda: {
            "resaleable": True,
            "exchange": True,
            "damaged": False,
            "obsolete": False,
            "not_received": False,
        }
    )
    # Handling cost estimates applied to every return line (labeled estimated).
    default_handling_cost_per_return_cents: int = Field(default=1500, ge=0)
    default_handling_cost_per_unit_cents: int = Field(default=50, ge=0)
    # Return freight expense estimate when no return freight log exists.
    default_return_freight_per_return_cents: int = Field(default=1200, ge=0)


# ---------------------------------------------------------------------------
# costing
# ---------------------------------------------------------------------------


class CostBasis(StrEnum):
    STANDARD = "standard"
    AVERAGE = "average"
    FIFO = "fifo"
    LIFO = "lifo"
    LANDED = "landed"


class CostingConfig(StrictModel):
    # Whether product cost (as mapped) already includes these components.
    # Must be declared in the mapping; these are engine-level fallbacks that
    # apply only when the mapping says nothing AND fail_on_missing_semantics
    # is False (not the default).
    missing_cost_policy: Literal["exclude_from_cost_metrics"] = "exclude_from_cost_metrics"
    zero_cost_policy: Literal["flag_for_review"] = "flag_for_review"
    vendor_rebate_treatment: Literal["adjust_acquisition_cost"] = "adjust_acquisition_cost"


# ---------------------------------------------------------------------------
# cost_to_serve
# ---------------------------------------------------------------------------


class CostToServeConfig(StrictModel):
    """Activity-based cost-to-serve rates (Phase 13), all in cents.

    Deliveries are proxied by orders when no delivery table exists (labeled).
    Return handling lives in the returns section; these rates cover ordinary
    order service activity.
    """

    per_order_cents: int = Field(default=1800, ge=0)
    per_line_cents: int = Field(default=150, ge=0)
    per_delivery_cents: int = Field(default=900, ge=0)
    special_handling_per_unit_cents: int = Field(default=0, ge=0)
    # Cost pools (Run 6, Phase 63): every activity rate belongs to exactly one
    # named pool, and a pool covered by an ACTUAL expense source must never
    # also be estimated by a rate. The engine resolves the default overlap -
    # the per-delivery rate proxies outbound freight, so it switches OFF
    # whenever an actual freight log exists (stated in the run notes); any
    # OTHER rate assigned to an actually-covered pool fails the run.
    activity_pools: dict[str, str] = Field(
        default_factory=lambda: {
            "per_order": "order_service",
            "per_line": "line_service",
            "per_delivery": "outbound_freight",
            "special_handling": "special_handling",
        }
    )
    # Service-policy flag thresholds (separate from pricing opportunities).
    min_order_value_cents: int = Field(default=50_000, ge=0)
    max_orders_per_month: float = Field(default=6.0, gt=0)

    @model_validator(mode="after")
    def _pools_cover_known_rates(self) -> CostToServeConfig:
        known = {"per_order", "per_line", "per_delivery", "special_handling"}
        bad = set(self.activity_pools) - known
        if bad:
            msg = f"cost_to_serve.activity_pools names unknown rates {sorted(bad)}; known: {sorted(known)}"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# waterfall
# ---------------------------------------------------------------------------


class AllocationBasis(StrEnum):
    ACTUAL = "actual"
    WEIGHT = "weight"
    CUBE = "cube"
    HANDLING_UNITS = "handling_units"
    LINES = "lines"
    EXTENDED_PRICE = "extended_price"


class WaterfallConfig(StrictModel):
    # Allocation basis priority: actual -> weight/cube -> handling units ->
    # lines/orders -> extended-price share (fallback only).
    allocation_basis_priority: tuple[AllocationBasis, ...] = (
        AllocationBasis.ACTUAL,
        AllocationBasis.WEIGHT,
        AllocationBasis.CUBE,
        AllocationBasis.HANDLING_UNITS,
        AllocationBasis.LINES,
        AllocationBasis.EXTENDED_PRICE,
    )
    rounding: Literal["half_even"] = "half_even"
    # Allocation method: largest-remainder (Hamilton) — floor each share to
    # the cent, hand remaining cents to the largest fractional remainders;
    # ties broken by larger absolute weight, then source_line_key.
    allocation_method: Literal["largest_remainder"] = "largest_remainder"
    # Freight: concessions reduce pocket revenue; actual outbound freight
    # expense is cost-to-serve. Fixed by accounting policy, surfaced here so
    # the report can state it.
    freight_concession_stage: Literal["pocket_revenue"] = "pocket_revenue"
    freight_expense_stage: Literal["cost_to_serve"] = "cost_to_serve"
    # Kit revenue allocation to components (Phase 12).
    kit_allocation_method: Literal["relative_standalone_price"] = "relative_standalone_price"


# ---------------------------------------------------------------------------
# outliers
# ---------------------------------------------------------------------------


class OutlierConfig(StrictModel):
    method: Literal["modified_robust_zscore"] = "modified_robust_zscore"
    zscore_threshold: float = Field(default=3.5, gt=0)
    mad_consistency_constant: float = 0.6745
    # Peer group: item x UOM x contract regime x quarter; family fallback below.
    peer_group_keys: tuple[str, ...] = ("item_id", "uom", "contract_regime", "quarter")
    min_peer_group_size: int = Field(default=30, ge=2)
    fallback_group_keys: tuple[str, ...] = ("item_family", "uom", "contract_regime", "quarter")
    # MAD = 0 fallback: flag prices deviating from the modal price by > 10%.
    mad_zero_fallback_pct: float = Field(default=0.10, gt=0)


# ---------------------------------------------------------------------------
# segmentation
# ---------------------------------------------------------------------------


class TreeConfig(StrictModel):
    max_depth: int = Field(ge=1)
    min_leaf_lines: int = Field(default=500, ge=1)
    min_leaf_lines_pct: float = Field(default=0.005, ge=0, le=1)
    min_leaf_customers: int = Field(default=20, ge=1)
    min_leaf_items: int = Field(default=10, ge=1)


class FeatureLineage(StrictModel):
    """Lineage metadata for one segmentation feature (Run 6, 60.4): where it
    comes from, how it is computed, over what window, and whether that window
    overlaps the outcome period. The leakage guard reads this metadata (not
    only name heuristics), so a feature built from outcome-period dollars is
    visible as such even when its name says nothing about price."""

    source_columns: tuple[str, ...]
    formula: str
    reference_window: str
    uses_outcome_period_data: bool


def _default_feature_lineage() -> dict[str, FeatureLineage]:
    static_customer = FeatureLineage(
        source_columns=("customers.*",),
        formula="customer master attribute, as mapped",
        reference_window="static attribute (not derived from transactions)",
        uses_outcome_period_data=False,
    )
    static_item = FeatureLineage(
        source_columns=("items.*",),
        formula="item master attribute, as mapped",
        reference_window="static attribute (not derived from transactions)",
        uses_outcome_period_data=False,
    )
    return {
        "industry": static_customer,
        "size_band": static_customer,
        "region": static_customer,
        "channel": static_customer,
        "contract_type": static_customer,
        "item_family": static_item,
        "item_role": static_item,
        "order_size_band": FeatureLineage(
            source_columns=("net_amount_cents", "order_id"),
            formula="terciles of order net value over the fitting sample",
            reference_window="fitting sample (same period as the target)",
            # Order net value embeds realized prices from the outcome period.
            uses_outcome_period_data=True,
        ),
        "revenue_abc": FeatureLineage(
            source_columns=("net_amount_cents", "customer_id"),
            formula="cumulative revenue-share classes A/B/C over the fitting sample",
            reference_window="fitting sample (same period as the target)",
            # Contemporaneous revenue embeds the target-period prices; Phase 76
            # replaces this with a pre-period ABC and flips this flag.
            uses_outcome_period_data=True,
        ),
    }


class SegmentationConfig(StrictModel):
    # Commercial segments: context features only — leakage guard.
    commercial_tree: TreeConfig = TreeConfig(max_depth=5)
    margin_driver_tree: TreeConfig = TreeConfig(max_depth=4)
    context_features: tuple[str, ...] = (
        "industry",
        "size_band",
        "region",
        "channel",
        "contract_type",
        "item_family",
        "item_role",
        "order_size_band",
        "revenue_abc",
    )
    # Columns that must never appear in commercial-segment features.
    forbidden_commercial_features: tuple[str, ...] = (
        "price",
        "discount",
        "cost",
        "rebate",
        "margin",
    )
    # Lineage metadata per feature (Run 6, 60.4): every context feature must
    # declare where it comes from and whether it uses outcome-period data.
    feature_lineage: dict[str, FeatureLineage] = Field(default_factory=_default_feature_lineage)
    bootstrap_resamples: int = Field(default=30, ge=2)
    # Cells whose mean normalized price index differs by less than this are not
    # commercially distinct and are consolidated into one segment.
    segment_merge_threshold: float = Field(default=0.02, gt=0)
    # ABC splits by cumulative share: A = top 80%, B = next 15%, C = last 5%.
    abc_a_share: float = 0.80
    abc_b_share: float = 0.15

    @model_validator(mode="after")
    def _lineage_covers_context_features(self) -> SegmentationConfig:
        missing = [f for f in self.context_features if f not in self.feature_lineage]
        if missing:
            msg = (
                f"segmentation: context features {missing} have no feature_lineage "
                "entry (Run 6, 60.4). Every segmentation feature must declare its "
                "source columns, formula, reference window and whether it uses "
                "outcome-period data."
            )
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# price_guidance
# ---------------------------------------------------------------------------


class PriceGuidanceConfig(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def _reject_renamed_margin_key(cls, data: object) -> object:
        if isinstance(data, dict) and "min_pocket_margin" in data:
            msg = (
                "price_guidance.min_pocket_margin was renamed to "
                "min_pocket_gross_margin (Run 4, 24.3) to make the margin "
                "definition explicit: (pocket revenue - product cost) / pocket "
                "revenue. Rename the key in rules.yaml."
            )
            raise ValueError(msg)
        return data

    band_percentiles: tuple[float, float, float] = (0.25, 0.50, 0.75)
    # Observation unit: one representative (median) price per customer x item x month.
    observation_unit: Literal["customer_item_month_median", "line"] = "customer_item_month_median"
    # Minimum POCKET GROSS margin (pocket revenue - product cost) / pocket
    # revenue. Renamed from `min_pocket_margin` in Run 4 (24.3) so the margin
    # definition is explicit; the old key is rejected with an actionable error.
    min_pocket_gross_margin: Decimal = Decimal("0.05")
    # Optional minimum CONTRIBUTION margin (pocket revenue - product cost -
    # cost-to-serve) / pocket revenue. When set, a second floor is computed:
    #   contribution invoice floor = ((C + K) / (1 - m_c) + F) / (1 - d)
    # with K = per-unit cost-to-serve (labeled estimated when it comes from
    # config activity rates). Guidance reports both floors and which binds.
    min_contribution_margin: Decimal | None = None
    # Floor rounding policy: floors are computed at full precision and rounded
    # ONCE at the end. "ceiling" (default) guarantees that a price at the floor
    # realizes at least the minimum margin; "half_even" can round a floor down
    # below the margin by a fraction of a cent.
    floor_rounding: Literal["ceiling", "half_even"] = "ceiling"
    min_band_observations: int = Field(default=10, ge=1)
    # Band independence (Run 6, Phase 67 / Finding 10): observations are
    # already independent units (one median per customer x item x month), and
    # a band additionally needs this many DISTINCT customers - a band built
    # from one customer's history is that customer's history, not a market
    # reference. Distinct customers, pairs, months and list regimes are
    # reported per band either way.
    min_band_customers: int = Field(default=5, ge=1)
    # Contribution-floor scope (Run 6, Phase 67): when min_contribution_margin
    # is set, apply it only to these item roles / channels (empty = all).
    # Order-type scoping would need an order-level floor, which does not exist
    # at pair grain - stated in the methodology as a limitation.
    contribution_floor_item_roles: tuple[str, ...] = ()
    contribution_floor_channels: tuple[str, ...] = ()
    # Roles whose below-floor prices are EXPLICITLY AUTHORIZED policy (loss
    # leaders, strategic key-value items, end-of-life clearances): their
    # below-floor statuses stay visible, but fix actions generate BLOCKED with
    # the authorization shown - a policy override exists only here, never
    # implicitly.
    authorized_below_floor_roles: tuple[str, ...] = ()
    # ERP price precision (Run 6, Phase 67): optimized and proposed prices are
    # rounded to this increment (cents); 1 = no constraint. Rounding never
    # goes below the binding floor.
    price_increment_cents: int = Field(default=1, ge=1)
    # Floor-index comparisons (floor / list vs realized index) require a valid
    # list price. A list in force but older than this many months at the pair's
    # last sale is STALE: the pair keeps dollar floors but gets no floor-index
    # comparison, and its list provenance says why (24.4). None disables.
    max_list_age_months: int | None = Field(default=24, ge=1)
    # Provisional target/start until optimization is supported (Phase 7).
    provisional_target_percentile: float = 0.50
    provisional_start_percentile: float = 0.75
    # Role-specific corridor policy (Phase 15): KVI corridors are pulled toward
    # the segment median by this factor (1.0 = no tightening).
    kvi_corridor_shrink: float = Field(default=0.5, gt=0, le=1.0)
    # Share of items labeled KVI *candidates* by the data-driven score.
    kvi_candidate_share: float = Field(default=0.15, gt=0, le=1.0)


# ---------------------------------------------------------------------------
# sensitivity
# ---------------------------------------------------------------------------


class InstrumentPath(StrictModel):
    """One plausible path from the instrument to quantity other than through
    the endogenous price (Run 6, 60.4): the channel (availability, lead times,
    supplier changes, competitor prices, expectations, substitution, ...) and
    how the analysis addresses it (a control, an argument for why it is
    absent, or an honest statement that it is unresolved)."""

    channel: str
    treatment: str
    # Machine-readable half of `treatment` (Run 6, Phase 70). The causal gate
    # reads this for the competitor_prices channel. Defaults to `unresolved`
    # so omitting documentation can never be rewarded, and `no_path` requires
    # a written treatment to count (enforced by the resolver).
    resolved_by: Literal["market_price_control", "fixed_effects", "no_path", "unresolved"] = (
        "unresolved"
    )
    # The grain at which THIS CHANNEL operates (Run 6, Phase 70), e.g.
    # "item_category:month" when the competitor-price condition a customer
    # faces is the market price for the item class rather than the supplier's
    # own. A control must be at least as fine as the CHANNEL's grain, not the
    # instrument's: an instrument can vary more finely than the confound it
    # travels through. Unset falls back to the instrument's own variation,
    # which is the conservative reading.
    channel_grain: str | None = None

    @model_validator(mode="after")
    def _no_path_needs_an_argument(self) -> InstrumentPath:
        if self.resolved_by == "no_path" and not self.treatment.strip():
            msg = (
                f"instrument path '{self.channel}' declares resolved_by: no_path but "
                "gives no treatment text. Claiming a channel does not exist requires "
                "a written argument - a one-word switch must not lift a causal gate."
            )
            raise ValueError(msg)
        return self


class InstrumentConfig(StrictModel):
    name: str
    column: str
    varies_at: str  # clustering level, e.g. "supplier_id:month" or "item_category:month"
    # Required for causal_elasticity_supported: a documented economic argument
    # for why the instrument is excludable. No rationale, no top status.
    exclusion_rationale: str | None = None
    # Plausible non-price paths to quantity and how each is addressed (Run 6,
    # 60.4). The causal gate reads the competitor-price path (Phase 70): an
    # instrument with no documented treatment of that path cannot support the
    # top status without a market-price control.
    paths_to_quantity: tuple[InstrumentPath, ...] = ()


class SensitivityConfig(StrictModel):
    panel_period: Literal["month"] = "month"
    # Zero rows only where the pair is in active assortment and item available:
    # purchased in the prior N months and not stocked out.
    active_assortment_lookback_months: int = Field(default=6, ge=1)
    min_first_stage_f: float = Field(default=10.0, gt=0)
    instruments: tuple[InstrumentConfig, ...] = ()
    robustness_max_spread: float = Field(default=0.20, gt=0)
    max_ci_width: float = Field(default=1.0, gt=0)
    overid_alpha: float = Field(default=0.05, gt=0, lt=1)
    # Cluster level for IV inference. "supplier_month" is where the cost
    # instrument varies (spec default); "month" is coarser and also absorbs
    # customer-month demand-shock correlation across suppliers — use it when
    # residuals are correlated across items a customer buys in the same month.
    # "month_customer" clusters two-way (month and customer), for customer
    # demand shocks that persist over time (Run 4, 24.5). The shipped default
    # in rules.yaml is chosen by the persistent-demand coverage study recorded
    # in validation.json, not here.
    cluster_level: Literal["supplier_month", "month", "month_customer"] = "supplier_month"
    # causal_elasticity_supported additionally requires the Anderson-Rubin set
    # to be bounded and no wider than this multiple of the primary CI (24.7).
    ar_max_width_ratio: float = Field(default=1.5, gt=0)
    # Market/competitor price control adequacy (Run 6, Phase 70). There is
    # deliberately NO switch to disable the competitor-price gate: the only
    # routes to the top status are an adequate control or an instrument whose
    # competitor-price path is documented as absent with a written argument.
    # Share of a segment's estimation rows the index must cover.
    market_control_min_coverage: float = Field(default=1.0, gt=0, le=1)
    # How many periods the index may stop short of the estimation window.
    market_control_max_staleness_periods: int = Field(default=1, ge=0)
    # RELEVANCE, not magnitude: the share of within-pair price movement the
    # index must explain to count as evidence of a competitor-price condition.
    # A control that explains nothing cannot hold anything constant - and a
    # faithful control is EXPECTED to correlate with industry-wide cost
    # instruments, so correlation is never itself disqualifying.
    market_control_min_price_r2: float = Field(default=0.01, ge=0, le=1)
    # Only NEAR-EXACT restatement of an instrument is refused: controlling for
    # it would remove the identifying variation rather than a confound.
    market_control_restatement_r2: float = Field(default=0.99, gt=0, le=1)
    # Commercial-outlier exclusion from the estimation panel requires at least
    # this many lines of pair history to be reliable (24.14; was hardcoded 6).
    outlier_exclusion_min_pair_lines: int = Field(default=6, ge=1)
    # Instrument builder (Run 4, 27.6): where the supplier cost instrument
    # comes from. "reference_table" uses a mapped supplier_cost_index table as
    # before; "receipts" builds it from purchase/receipt history (log change
    # in qty-weighted average purchase cost per supplier-month, demeaned);
    # "external" maps a public series (e.g. BLS PPI) to categories via
    # external_index_map and to suppliers via their modal category.
    instrument_source: Literal["reference_table", "receipts", "external"] = "reference_table"
    external_index_map: dict[str, str] = Field(default_factory=dict)
    # Validation without planted truth (Run 4, 27.4): temporal backtest,
    # placebo tests and rolling-window stability, reported in the "Real-data
    # validation" report section. All informational, never gated.
    real_data_validation: bool = True
    backtest_holdout_months: int = Field(default=6, ge=1)
    placebo_draws: int = Field(default=10, ge=2)
    stability_window_months: int = Field(default=12, ge=3)
    stability_step_months: int = Field(default=6, ge=1)
    # Above this many positive rows, a segment's placebo diagnostics run on a
    # seeded pair subsample (labeled in the output). Headline estimates never
    # sample.
    real_validation_max_rows: int = Field(default=200_000, ge=10_000)
    shrinkage: Literal["empirical_bayes_to_parent_segment"] = "empirical_bayes_to_parent_segment"
    min_observations: int = Field(default=30, ge=2)
    # IV-based statuses (iv_estimated / causal) additionally require this much
    # support; below it the estimate is reported but capped at descriptive.
    min_iv_observations: int = Field(default=200, ge=2)
    min_iv_pairs: int = Field(default=50, ge=2)
    # Win-curve estimation (Phase 16): segments with fewer quotes get
    # insufficient_data and are excluded from accuracy gating.
    min_quotes_per_segment: int = Field(default=400, ge=10)
    # A fitted win curve carries estimated status only when its own relative
    # standard error (se / |coefficient|) is at or under this; noisier fits are
    # insufficient_precision -- reported with their CI, never accuracy-gated.
    win_curve_max_rel_se: float = Field(default=0.10, gt=0)
    min_price_variation_cv: float = Field(default=0.01, ge=0)


# ---------------------------------------------------------------------------
# opportunity
# ---------------------------------------------------------------------------


class OpportunityConfig(StrictModel):
    baseline_window_months: int = Field(default=12, ge=1)
    exclude_contract_protected: bool = True
    exclude_insufficient_data: bool = True
    # Expected contribution change only at causal_elasticity_supported;
    # iv_estimated shown as a labeled sensitivity range; else fixed-volume only.
    volume_response_requires: Literal["causal_elasticity_supported"] = "causal_elasticity_supported"
    # Optimizer objective (Run 6, Phase 65): pocket gross profit
    # (P(1-d) - F - C) or contribution (also subtracts per-unit cost-to-serve
    # K, estimated from activity rates). Every optimizer output names its
    # objective; a pair without a supported K keeps the provisional target
    # under the contribution objective instead of borrowing a generic number.
    optimizer_objective: Literal["pocket_gross_profit", "contribution"] = "pocket_gross_profit"
    # How per-order and per-delivery costs respond to a volume change (Run 6,
    # Phase 65 / Finding 8). fixed_order_frequency: customers keep their
    # ordering cadence, so order quantities scale with volume and the TOTAL
    # per-order cost is volume-constant - it drops out of marginal economics.
    # fixed_order_size: order counts scale with volume, so per-order cost is
    # marginal per unit. The contribution optimizer re-optimizes numerically
    # under the declared response.
    order_cost_response: Literal["fixed_order_frequency", "fixed_order_size"] = (
        "fixed_order_frequency"
    )


# ---------------------------------------------------------------------------
# tariff_scenarios
# ---------------------------------------------------------------------------


class TariffScenariosConfig(StrictModel):
    pass_through_levels: tuple[float, ...] = (0.0, 0.5, 1.0)
    exclude_contract_protected: bool = True
    # Run 2 (Phase 14): which cost pool the scenario hits.
    cost_pool: Literal["new_receipts", "existing_pool", "blend"] = "new_receipts"


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


class ReportingConfig(StrictModel):
    output_formats: tuple[Literal["parquet", "csv", "html"], ...] = ("parquet", "csv", "html")
    show_evidence_status: bool = True
    show_data_coverage: bool = True
    # Books tie-out (Run 4, 27.3): a month fails when the engine-vs-GL
    # difference exceeds BOTH bounds (the cents floor keeps tiny months from
    # failing on rounding pennies). Failing months are flagged, never hidden.
    tieout_tolerance_pct: float = Field(default=0.5, ge=0)
    tieout_tolerance_cents: int = Field(default=100, ge=0)


# ---------------------------------------------------------------------------
# drift monitoring (Run 5, Phase 43)
# ---------------------------------------------------------------------------


class DriftConfig(StrictModel):
    """Live-mode drift thresholds: each refresh compares against the previous
    run, and only moves beyond these bounds are flagged (plain-English, in the
    API and UI)."""

    enabled: bool = True
    # Segment-assignment agreement (adjusted Rand index over shared lines).
    ari_warn_below: float = Field(default=0.8, ge=0.0, le=1.0)
    min_common_lines: int = Field(default=100, ge=1)
    # Absolute move of a segment's IV elasticity estimate.
    elasticity_abs_change: float = Field(default=0.3, ge=0.0)
    # Data-quality rate change, in percentage points.
    data_quality_change_pp: float = Field(default=2.0, ge=0.0)
    # Guidance status count change: relative share AND an absolute floor so
    # tiny statuses do not flap.
    status_share_change: float = Field(default=0.2, ge=0.0)
    min_status_count_change: int = Field(default=5, ge=1)


# ---------------------------------------------------------------------------
# actions (Run 5, Phase 38)
# ---------------------------------------------------------------------------


class ActionPriorityConfig(StrictModel):
    """Default priority rule: by expected dollar impact, demoted one level when
    the volume evidence is not causal-supported (configurable)."""

    high_impact_cents: int = Field(default=500_000, ge=0)
    medium_impact_cents: int = Field(default=50_000, ge=0)
    demote_without_causal: bool = True


class PriceExportConfig(StrictModel):
    """ERP-ready price file template (Phase 40). The generic CSV is the
    default; a deployment overrides header names, date format, decimal
    separator and price unit to match what its ERP imports."""

    delimiter: str = Field(default=",", min_length=1, max_length=1)
    decimal_separator: Literal[".", ","] = "."
    date_format: str = "%Y-%m-%d"
    price_unit: Literal["dollars", "cents"] = "dollars"
    headers: dict[str, str] = Field(
        default_factory=lambda: {
            "action_id": "ACTION_ID",
            "approval_id": "APPROVAL_ID",
            "customer_id": "CUSTOMER",
            "item_id": "ITEM",
            "uom": "UOM",
            "effective_date": "EFFECTIVE_DATE",
            "previous_price": "PREVIOUS_PRICE",
            "new_price": "NEW_PRICE",
        }
    )

    @model_validator(mode="after")
    def _headers_complete(self) -> PriceExportConfig:
        required = {
            "action_id",
            "approval_id",
            "customer_id",
            "item_id",
            "uom",
            "effective_date",
            "previous_price",
            "new_price",
        }
        missing = required - set(self.headers)
        if missing:
            msg = (
                f"price_export.headers must name every canonical column; missing {sorted(missing)}"
            )
            raise ValueError(msg)
        if self.decimal_separator == "," and self.delimiter == ",":
            msg = (
                "price_export: a comma decimal separator needs a non-comma "
                "delimiter (e.g. ';'), or the file is unparseable"
            )
            raise ValueError(msg)
        return self


class RevalidationConfig(StrictModel):
    """Stale-action policy (Run 6, Phase 69). A proposal is a claim about the
    world at generation time; approval and export revalidate it against the
    engine's current outputs. Moves beyond these thresholds send it back for
    re-review rather than letting a stale number through."""

    enabled: bool = True
    # Relative moves (fraction) beyond which re-review is required.
    floor_change_pct: float = Field(default=0.02, ge=0)
    impact_change_pct: float = Field(default=0.20, ge=0)
    current_price_change_pct: float = Field(default=0.02, ge=0)
    # A proposal older than this many days is expired: it must be regenerated
    # or re-reviewed, never approved on its original evidence.
    expiry_days: int = Field(default=90, ge=1)


class ActionsConfig(StrictModel):
    """Action generation (Phase 38). Dates are anchored to the data's last
    invoice date so generation is deterministic for a given world."""

    enabled: bool = True
    default_owner: str = "unassigned"
    priority: ActionPriorityConfig = ActionPriorityConfig()
    due_days_high: int = Field(default=14, ge=0)
    due_days_medium: int = Field(default=30, ge=0)
    due_days_low: int = Field(default=60, ge=0)
    # Cost pass-through actions: the chosen level applied to the auto-selected
    # top scenarios (never assumed full pass-through).
    pass_through_level: float = Field(default=0.5, ge=0.0, le=1.0)
    max_pass_through_scenarios: int = Field(default=2, ge=0)
    # Rebate/discount review: a customer's leakage rate must exceed BOTH the
    # ratio to its segment's median rate and the absolute excess-dollar floor.
    leakage_review_min_ratio: float = Field(default=1.5, ge=1.0)
    leakage_review_min_excess_cents: int = Field(default=100_000, ge=0)
    # Deterministic cap per action type (largest expected impact first).
    max_actions_per_type: int = Field(default=200, ge=1)
    price_export: PriceExportConfig = PriceExportConfig()
    # Results tracking (Phase 41): measurement window after implementation,
    # evidence gates for the difference-in-differences comparison, and the
    # minimum measured actions before a calibration number is shown.
    tracking_window_days: int = Field(default=90, ge=1)
    tracking_min_control_pairs: int = Field(default=5, ge=1)
    tracking_min_after_sales: int = Field(default=3, ge=1)
    calibration_min_actions: int = Field(default=10, ge=1)
    # Stale-action revalidation and expiry (Run 6, Phase 69).
    revalidation: RevalidationConfig = RevalidationConfig()


# ---------------------------------------------------------------------------
# pricing compliance (Run 5, Phase 37)
# ---------------------------------------------------------------------------


class RobinsonPatmanConfig(StrictModel):
    """Review flag for price discrimination exposure: customers who are likely
    COMPETITORS of each other (same values on `competitor_attributes`, default
    industry + region) receiving recommended prices for the SAME item that
    differ by more than the threshold. The flag is a review prompt with the
    cost-to-serve difference shown as supporting documentation (cost
    justification is a recognized defense) — orientation, not legal advice.
    """

    enabled: bool = True
    competitor_attributes: tuple[str, ...] = ("industry", "region")
    # Relative difference between the two recommended prices, in percent;
    # strictly-greater-than fires the flag.
    price_difference_threshold_pct: float = Field(default=5.0, ge=0)


class ComplianceConfig(StrictModel):
    robinson_patman: RobinsonPatmanConfig = RobinsonPatmanConfig()
    # The market/competitor price index control is accepted from PUBLIC
    # sources only (the mapping must declare public_source: true): feeding
    # competitors' non-public prices through a pricing algorithm raises
    # antitrust concerns, so the engine refuses the control rather than
    # assume. Documented in docs/methodology.md (Pricing-law considerations).
    market_index_public_source_required: bool = True


# ---------------------------------------------------------------------------
# fiscal calendar (Run 5, Phase 36)
# ---------------------------------------------------------------------------


class FiscalConfig(StrictModel):
    """Fiscal calendar for the engine's ANALYTIC periods: the sensitivity
    panel, peer-group quarters, the guidance observation unit, baseline
    windows, period counts and the books tie-out. Data-declared periods
    (rebate settlement periods, landed-cost months, FX rate months) always
    follow the source data and are untouched by this setting.

    calendar_months (default) reproduces the engine's historical behavior
    exactly. The week-based calendars (4-4-5 / 4-5-4 / 5-4-4) define a fiscal
    year as the 52 or 53 weeks ending on `year_end_weekday` nearest to the
    last day of `year_end_month` (the retail-calendar convention); in a
    53-week year the extra week extends the year's final period. `custom`
    takes an explicit period table (reference table `fiscal_periods` in the
    mapping: period_label, start_date, end_date, optional quarter_label).
    """

    calendar: Literal["calendar_months", "4-4-5", "4-5-4", "5-4-4", "custom"] = "calendar_months"
    year_end_month: int = Field(default=12, ge=1, le=12)
    year_end_weekday: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ] = "saturday"


# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------


class Rules(StrictModel):
    """Root model for rules.yaml."""

    schema_: SchemaConfig = Field(default=SchemaConfig(), alias="schema")
    mapping: MappingConfig = MappingConfig()
    units_and_currency: UnitsCurrencyConfig = UnitsCurrencyConfig()
    transactions: TransactionsConfig = TransactionsConfig()
    returns: ReturnsConfig = ReturnsConfig()
    hierarchy: HierarchyConfig = HierarchyConfig()
    cost_to_serve: CostToServeConfig = CostToServeConfig()
    costing: CostingConfig = CostingConfig()
    waterfall: WaterfallConfig = WaterfallConfig()
    outliers: OutlierConfig = OutlierConfig()
    segmentation: SegmentationConfig = SegmentationConfig()
    price_guidance: PriceGuidanceConfig = PriceGuidanceConfig()
    sensitivity: SensitivityConfig = SensitivityConfig()
    opportunity: OpportunityConfig = OpportunityConfig()
    tariff_scenarios: TariffScenariosConfig = TariffScenariosConfig()
    reporting: ReportingConfig = ReportingConfig()
    fiscal: FiscalConfig = FiscalConfig()
    compliance: ComplianceConfig = ComplianceConfig()
    actions: ActionsConfig = ActionsConfig()
    drift: DriftConfig = DriftConfig()

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _abc_shares_valid(self) -> Rules:
        seg = self.segmentation
        if seg.abc_a_share + seg.abc_b_share >= 1.0:
            msg = "segmentation: abc_a_share + abc_b_share must be < 1.0"
            raise ValueError(msg)
        return self


def load_rules(path: str | Path) -> Rules:
    """Load and schema-validate rules.yaml. Raises on unknown keys or bad values."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        msg = f"rules file {path} must be a YAML mapping, got {type(raw).__name__}"
        raise TypeError(msg)
    return Rules.model_validate(raw)
