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
    # Service-policy flag thresholds (separate from pricing opportunities).
    min_order_value_cents: int = Field(default=50_000, ge=0)
    max_orders_per_month: float = Field(default=6.0, gt=0)


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
    bootstrap_resamples: int = Field(default=30, ge=2)
    # Cells whose mean normalized price index differs by less than this are not
    # commercially distinct and are consolidated into one segment.
    segment_merge_threshold: float = Field(default=0.02, gt=0)
    # ABC splits by cumulative share: A = top 80%, B = next 15%, C = last 5%.
    abc_a_share: float = 0.80
    abc_b_share: float = 0.15


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


class InstrumentConfig(StrictModel):
    name: str
    column: str
    varies_at: str  # clustering level, e.g. "supplier_id:month" or "item_category:month"
    # Required for causal_elasticity_supported: a documented economic argument
    # for why the instrument is excludable. No rationale, no top status.
    exclusion_rationale: str | None = None


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
    # Commercial-outlier exclusion from the estimation panel requires at least
    # this many lines of pair history to be reliable (24.14; was hardcoded 6).
    outlier_exclusion_min_pair_lines: int = Field(default=6, ge=1)
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
