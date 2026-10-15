"""Mapping schema: column names AND business meaning, validated up front.

A mapping YAML declares, for every amount field: currency, sign convention,
whether discounts are embedded, cost basis and inclusions, date semantics and
reversal handling. If a calculation needs a semantic the mapping does not
declare, ingestion fails with an actionable error naming the field, the missing
semantic, and the calculation that needs it. The engine never guesses.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MappingError(ValueError):
    """Actionable mapping/semantics failure. Message says what to fix."""


class SignConvention(StrEnum):
    NATURAL = "natural"  # sales positive, returns/credits negative
    POSITIVE_REDUCES_REVENUE = "positive_reduces_revenue"  # deductions stored positive
    ALWAYS_POSITIVE_WITH_TYPE = "always_positive_with_type"  # sign via txn type


class CostBasisDecl(StrEnum):
    STANDARD = "standard"
    AVERAGE = "average"
    FIFO = "fifo"
    LANDED = "landed"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyField(StrictModel):
    """Identifier / dimension column: no money semantics needed."""

    column: str


class DateField(StrictModel):
    column: str
    format: str  # strptime format; never guessed


class QuantityField(StrictModel):
    column: str
    # UOM is carried by the uom field; quantity itself just needs the column.


class AmountField(StrictModel):
    """A money column. Currency and sign convention are mandatory."""

    column: str
    currency: str
    sign_convention: SignConvention
    # Only meaningful for revenue-like amounts.
    discounts_embedded: bool | None = None


class UnitPriceField(StrictModel):
    """Informational unit price; net_amount is authoritative when both exist."""

    column: str
    currency: str
    authoritative: bool = False


class CostField(StrictModel):
    """Unit cost. Basis and inclusion flags are mandatory: a column called
    'Cost' with no declared basis is exactly the ambiguity we refuse to guess."""

    column: str
    currency: str
    cost_basis: CostBasisDecl
    includes_inbound_freight: bool
    includes_duty: bool
    includes_vendor_rebates: bool
    # Which unit the cost is per: the line's selling UOM as invoiced, or the
    # item's base UOM. Mandatory — mixed-UOM exports differ and we never guess.
    per_uom: Literal["line_uom", "base_uom"]
    missing_as: Literal["blank", "zero_is_missing"] = "blank"


class TransactionTypeField(StrictModel):
    column: str
    # Source code -> canonical type (sale, return, credit).
    code_map: dict[str, Literal["sale", "return", "credit", "free_good"]]
    # How reversals are represented: own transaction code vs. negative amounts.
    reversal_handling: Literal["own_code", "negative_amounts"] = "own_code"


class LinesMapping(StrictModel):
    """Field mappings for the invoice-lines file."""

    source_line_key: KeyField
    invoice_id: KeyField
    invoice_date: DateField
    customer_id: KeyField
    item_id: KeyField
    quantity: QuantityField
    uom: KeyField | None = None
    net_amount: AmountField | None = None
    unit_price: UnitPriceField | None = None
    unit_cost: CostField | None = None
    transaction_type: TransactionTypeField | None = None
    # Optional fields (each unlocks features).
    order_id: KeyField | None = None
    list_price: UnitPriceField | None = None
    on_invoice_discount: AmountField | None = None
    customer_rebate_amount: AmountField | None = None
    rebate_program_id: KeyField | None = None
    freight_concession: AmountField | None = None
    # Return economics (phase-8.2): actual restocking fee charged back to the
    # customer and return freight billed to the customer, both recovery revenue.
    restocking_fee: AmountField | None = None
    return_freight_billed: AmountField | None = None
    credit_reason: KeyField | None = None
    original_invoice_ref: KeyField | None = None
    sales_rep_id: KeyField | None = None
    channel: KeyField | None = None
    contract_id: KeyField | None = None
    currency: KeyField | None = None
    kit_parent_id: KeyField | None = None

    @model_validator(mode="after")
    def _price_or_net(self) -> LinesMapping:
        if self.net_amount is None and self.unit_price is None:
            msg = (
                "mapping must declare unit_price or net_amount for invoice lines. "
                "Add a 'net_amount' (preferred, authoritative) or 'unit_price' entry."
            )
            raise MappingError(msg)
        if (
            self.net_amount is None
            and self.unit_price is not None
            and not self.unit_price.authoritative
        ):
            msg = (
                "no net_amount is mapped, so unit_price must declare authoritative: true "
                "(net will be computed as quantity x unit_price)."
            )
            raise MappingError(msg)
        return self


class ReferenceTable(StrictModel):
    file: str
    columns: dict[str, str]  # canonical name -> source column
    date_format: str | None = None


class SourceMapping(StrictModel):
    """Root of a mapping YAML for one source system."""

    source_system: str
    lines_file: str
    decimal_separator: Literal[".", ","] = "."
    lines: LinesMapping
    # Optional reference tables, keyed by canonical table name.
    reference_tables: dict[str, ReferenceTable] = Field(default_factory=dict)
    # Credit-memo reason normalization: source code -> canonical reason.
    credit_reason_map: dict[str, str] = Field(default_factory=dict)
    # Return reason normalization: source code -> canonical return reason
    # (customer_error, distributor_error, defective). Unmapped codes become
    # "unspecified" and receive only estimated default handling costs.
    return_reason_map: dict[str, str] = Field(default_factory=dict)

    def require(self, field: str, needed_for: str) -> None:
        """Fail actionably if an optional field needed by a calculation is absent."""
        if getattr(self.lines, field, None) is None:
            msg = (
                f"mapping '{self.source_system}' does not map '{field}', which is "
                f"required for: {needed_for}. Add a '{field}' entry under 'lines' "
                f"in the mapping YAML (with its semantics), or disable the feature."
            )
            raise MappingError(msg)


KNOWN_REFERENCE_TABLES = {
    "price_list",
    "uom_conversions",
    "customers",
    "items",
    "contracts",
    "rebate_programs",
    "rebate_settlements",
    "order_charges",
    "freight_log",
    "vendor_rebate_programs",
    "landed_cost_components",
    "supplier_cost_index",
    "tariff_schedule",
    "return_freight_log",
    "fx_rates",
    "rebate_program_tiers",
    "tiered_settlements",
    "inventory_receipts",
    "quotes",
    "substitution_groups",
}


def load_mapping(path: str | Path) -> SourceMapping:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"mapping file {path} must be a YAML mapping"
        raise MappingError(msg)
    try:
        mapping = SourceMapping.model_validate(raw)
    except MappingError:
        raise
    except Exception as exc:  # pydantic ValidationError -> actionable message
        msg = (
            f"mapping file {path} failed schema validation. Every amount field "
            f"must declare currency and sign_convention; unit_cost must declare "
            f"cost_basis and inclusion flags. Details:\n{exc}"
        )
        raise MappingError(msg) from exc
    unknown = set(mapping.reference_tables) - KNOWN_REFERENCE_TABLES
    if unknown:
        msg = (
            f"mapping declares unknown reference tables {sorted(unknown)}; "
            f"known tables: {sorted(KNOWN_REFERENCE_TABLES)}"
        )
        raise MappingError(msg)
    return mapping
