"""Phase 63 (Run 6): freight, charges, taxes and non-overlapping cost pools.

Hand-calculable fixtures: the realized price never carries billed charges or
embedded taxes; excluded-liability charges never enter product revenue; a cost
pool is never counted twice; return dispositions control cost recovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from margin_engine.config import Rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.extended.cost_to_serve import (
    CostPoolOverlapError,
    resolve_activity_rates,
    run_cost_to_serve,
)
from margin_engine.ingest.mapping import MappingError, load_mapping
from margin_engine.ledger.events import build_events
from margin_engine.ledger.pipeline import PipelineResult, run_pipeline
from tests.fixtures.builder import FixtureWorld

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"


def _mapping_with(tmp_path: Path, **tweaks: Any) -> Path:
    raw = yaml.safe_load(MAPPING.read_text(encoding="utf-8"))
    for key, value in tweaks.items():
        if key == "lines":
            raw["lines"].update(value)
        else:
            raw[key] = value
    out = tmp_path / "mapping_tweaked.yaml"
    out.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return out


def _sale(w: FixtureWorld, **kw: Any) -> None:
    w.add_item("X1")
    w.add_list_price("X1", "01/01/2024", "100.00")
    base: dict[str, Any] = {
        "INV_NO": "INV1",
        "CUST_NO": "C1",
        "ITEM_NO": "X1",
        "QTY_SHP": 10,
        "UNIT_PRC": "95.00",
        "EXT_NET": "950.00",
        "LIST_PRC": "100.00",
        "UNIT_CST": "60.0000",
    }
    base.update(kw)
    w.add_line(**base)


def _run(
    world: FixtureWorld, mapping: Path = MAPPING, rules: Rules | None = None
) -> PipelineResult:
    res = run_pipeline(world.write(), mapping, rules or Rules())
    assert res.reconciliation.all_pass
    return res


# ---------------------------------------------------------------------------
# Price index basis: billed charges never enter the realized price.
# ---------------------------------------------------------------------------


def test_billed_freight_never_enters_price_index(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    _sale(w)
    # A large billed freight charge on the same order.
    w.order_charges = [{"ORD_NO": "ORD1", "CHG_CD": "FRT_OUT", "AMT": "100.00"}]
    res = _run(w)
    row = res.ledger.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    # The charge IS revenue (allocated into invoice revenue)...
    assert row["billed_charge_alloc_cents"] == 10_000
    assert row["invoice_revenue_cents"] == 95_000 + 10_000
    # ...but the realized price index is net product / quantity / list: 0.95.
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], Rules())
    assert flagged.filter(pl.col("source_line_key") == "L1")["price_index"][0] == pytest.approx(
        0.95
    )


# ---------------------------------------------------------------------------
# Taxes: excluded from product revenue, embedded or billed separately.
# ---------------------------------------------------------------------------

_TAX_EMBEDDED = {
    "tax_amount": {
        "column": "TAX_AMT",
        "currency": "line_currency",
        "sign_convention": "natural",
        "embedded_in_net": True,
    }
}


def test_embedded_tax_carved_out_of_product_revenue(tmp_path: Path) -> None:
    mapping = _mapping_with(tmp_path, lines=_TAX_EMBEDDED)
    w = FixtureWorld(tmp_path)
    _sale(w, TAX_AMT="50.00")  # the 950.00 net INCLUDES 50.00 of tax
    res = _run(w, mapping)
    row = res.ledger.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    assert row["tax_collected_cents"] == 5_000
    assert row["net_product_cents"] == 90_000
    assert row["invoice_revenue_cents"] == 90_000  # tax never in product revenue
    assert row["pocket_revenue_cents"] == 90_000
    assert row["pocket_gp_cents"] == 90_000 - 60_000
    # Price index on the tax-free product amount: 900 / 10 / 100 = 0.90.
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], Rules())
    assert flagged.filter(pl.col("source_line_key") == "L1")["price_index"][0] == pytest.approx(
        0.90
    )
    ev = build_events(res.ledger, rules=Rules())
    tax = ev.events.filter(pl.col("event_type") == "tax_deposit_liability").to_dicts()[0]
    assert tax["amount_cents"] == 5_000
    assert tax["counterparty"] == "tax_authority"


def test_separately_billed_tax_never_added_to_revenue(tmp_path: Path) -> None:
    tweak = {"tax_amount": {**_TAX_EMBEDDED["tax_amount"], "embedded_in_net": False}}
    mapping = _mapping_with(tmp_path, lines=tweak)
    w = FixtureWorld(tmp_path)
    _sale(w, TAX_AMT="50.00")  # billed on top; net 950.00 is tax-free already
    res = _run(w, mapping)
    row = res.ledger.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    assert row["tax_collected_cents"] == 5_000
    assert row["net_product_cents"] == 95_000
    assert row["invoice_revenue_cents"] == 95_000
    assert any("billed separately" in n for n in res.notes)


def test_tax_without_embedded_declaration_is_refused(tmp_path: Path) -> None:
    tweak = {
        "tax_amount": {
            k: v for k, v in _TAX_EMBEDDED["tax_amount"].items() if k != "embedded_in_net"
        }
    }
    mapping = _mapping_with(tmp_path, lines=tweak)
    with pytest.raises(MappingError, match="embedded_in_net"):
        load_mapping(mapping)


# ---------------------------------------------------------------------------
# Charge declarations: pools, excluded liabilities, event typing.
# ---------------------------------------------------------------------------

_CHARGE_DECLS = {
    "FRT_OUT": {
        "economic_owner": "customer",
        "direction": "revenue_increase",
        "pool": "pass_through_revenue",
        "amount_basis": "per order",
    },
    "DEPOSIT": {
        "economic_owner": "tax_authority",
        "direction": "revenue_increase",
        "pool": "excluded_liability",
        "amount_basis": "per order",
    },
}


def test_excluded_liability_charge_never_enters_revenue(tmp_path: Path) -> None:
    mapping = _mapping_with(tmp_path, charge_declarations=_CHARGE_DECLS)
    w = FixtureWorld(tmp_path)
    _sale(w)
    w.order_charges = [
        {"ORD_NO": "ORD1", "CHG_CD": "FRT_OUT", "AMT": "20.00"},
        {"ORD_NO": "ORD1", "CHG_CD": "DEPOSIT", "AMT": "30.00"},
    ]
    res = _run(w, mapping)
    row = res.ledger.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    # Only the pass-through freight reaches invoice revenue; the deposit is a
    # visible liability, and the reconciliation still ties the table total.
    assert row["billed_charge_alloc_cents"] == 2_000
    assert row["invoice_revenue_cents"] == 95_000 + 2_000
    assert res.reconciliation.residuals["billed_charges_excluded_liability_cents"] == 3_000
    check = next(c for c in res.reconciliation.checks if c["check"] == "billed_charges_allocation")
    assert check["pass"] and check["source_total_cents"] == 5_000
    mapping_obj = load_mapping(mapping)
    ev = build_events(
        res.ledger,
        rules=Rules(),
        order_charges=res.reference["order_charges"],
        charge_declarations=mapping_obj.charge_declarations,
    )
    frt = ev.events.filter(pl.col("event_type") == "freight_pass_through").to_dicts()[0]
    assert frt["amount_cents"] == 2_000
    dep = ev.events.filter(pl.col("event_type") == "tax_deposit_liability").to_dicts()[0]
    assert dep["amount_cents"] == 3_000
    assert dep["counterparty"] == "tax_authority"


def test_charge_declaration_never_embedded(tmp_path: Path) -> None:
    decls = {
        "FRT_OUT": {**_CHARGE_DECLS["FRT_OUT"], "embedded_in_net": True},
    }
    mapping = _mapping_with(tmp_path, charge_declarations=decls)
    with pytest.raises(MappingError, match="embedded_in_net"):
        load_mapping(mapping)


# ---------------------------------------------------------------------------
# Cost pools: never counted twice.
# ---------------------------------------------------------------------------


def test_per_delivery_rate_switched_off_with_actual_freight(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    _sale(w)
    w.freight_log = [{"ORD_NO": "ORD1", "FRT_EXP_AMT": "15.00"}]
    res = _run(w)
    cts = run_cost_to_serve(res.ledger, Rules(), actual_pools=frozenset({"outbound_freight"}))
    row = cts.lines.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    # per_order 18.00 + per_line 1.50; the 9.00 per-delivery estimate is OFF
    # because the actual freight expense covers that pool.
    assert row["activity_cts_cents"] == 1_800 + 150
    assert any("switched OFF" in n for n in cts.notes)
    assert cts.pools is not None
    freight_pool = cts.pools.filter(pl.col("cost_pool_id") == "outbound_freight").to_dicts()[0]
    assert freight_pool["covered_by"] == "actual_expense"


def test_without_actual_freight_the_estimate_applies(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    _sale(w)
    res = _run(w)
    cts = run_cost_to_serve(res.ledger, Rules(), actual_pools=frozenset())
    row = cts.lines.filter(pl.col("source_line_key") == "L1").to_dicts()[0]
    assert row["activity_cts_cents"] == 1_800 + 900 + 150


def test_other_pool_overlap_fails_the_run() -> None:
    rules = Rules.model_validate(
        {
            "cost_to_serve": {
                "activity_pools": {
                    "per_order": "outbound_freight",  # deliberately wrong
                    "per_line": "line_service",
                    "per_delivery": "outbound_freight",
                    "special_handling": "special_handling",
                }
            }
        }
    )
    with pytest.raises(CostPoolOverlapError, match="counted twice"):
        resolve_activity_rates(rules, frozenset({"outbound_freight"}))


# ---------------------------------------------------------------------------
# Return dispositions: whether product cost is recovered.
# ---------------------------------------------------------------------------

_DISP_MAPPING = {
    "lines": {"return_disposition": {"column": "RTN_DISP"}},
    "return_disposition_map": {"DMG": "damaged", "OK": "resaleable"},
}


def _world_with_return(tmp_path: Path, disp: str) -> FixtureWorld:
    w = FixtureWorld(tmp_path)
    _sale(w)
    w.add_line(
        INV_NO="RET1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-2,
        UNIT_PRC="95.00",
        EXT_NET="-190.00",
        TXN_CD="R",
        ORIG_INV="INV1",
        UNIT_CST="60.0000",
        RTN_DISP=disp,
    )
    return w


def test_damaged_return_recovers_no_cost(tmp_path: Path) -> None:
    mapping = _mapping_with(tmp_path, **_DISP_MAPPING)
    res = _run(_world_with_return(tmp_path, "DMG"), mapping)
    ret = res.ledger.filter(pl.col("source_line_key") == "L2").to_dicts()[0]
    assert ret["return_disposition"] == "damaged"
    assert ret["return_cost_recovered"] is False
    # No cost comes back: the loss stays in cost of goods sold.
    assert ret["product_cost_cents"] == 0
    assert ret["pocket_gp_cents"] == ret["pocket_revenue_cents"]


def test_resaleable_return_recovers_cost(tmp_path: Path) -> None:
    mapping = _mapping_with(tmp_path, **_DISP_MAPPING)
    res = _run(_world_with_return(tmp_path, "OK"), mapping)
    ret = res.ledger.filter(pl.col("source_line_key") == "L2").to_dicts()[0]
    assert ret["return_cost_recovered"] is True
    assert ret["product_cost_cents"] == -12_000  # 2 units x $60 back into stock


def test_unknown_disposition_recovers_with_a_note(tmp_path: Path) -> None:
    mapping = _mapping_with(tmp_path, **_DISP_MAPPING)
    res = _run(_world_with_return(tmp_path, "MYSTERY"), mapping)
    ret = res.ledger.filter(pl.col("source_line_key") == "L2").to_dicts()[0]
    assert ret["return_cost_recovered"] is True
    assert any("no cost-recovery rule" in n for n in res.notes)


def test_without_disposition_column_recovery_is_the_stated_default(tmp_path: Path) -> None:
    w = FixtureWorld(tmp_path)
    _sale(w)
    w.add_line(
        INV_NO="RET1",
        CUST_NO="C1",
        ITEM_NO="X1",
        QTY_SHP=-2,
        UNIT_PRC="95.00",
        EXT_NET="-190.00",
        TXN_CD="R",
        ORIG_INV="INV1",
        UNIT_CST="60.0000",
    )
    res = _run(w)
    ret = res.ledger.filter(pl.col("source_line_key") == "L2").to_dicts()[0]
    assert ret["return_cost_recovered"] is True
    assert ret["product_cost_cents"] == -12_000
