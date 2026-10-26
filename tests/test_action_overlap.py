"""Phase 68 (Run 6): action overlap and portfolio totals.

One active proposed price per customer x item x UOM x effective date;
cross-scope overlaps share a group whose joint value is a portfolio estimate
under one baseline - never a naive sum.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from margin_engine.actions.export import build_price_file
from margin_engine.actions.generate import ACTIONS_SCHEMA
from margin_engine.actions.model import append_event, empty_events
from margin_engine.actions.portfolio import build_portfolio_groups
from margin_engine.actions.review import ReviewError, check_no_duplicate_active_price
from margin_engine.config import Rules
from margin_engine.scenarios.tariff import CostShock, pass_through_grid


def _action(action_id: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = dict.fromkeys(ACTIONS_SCHEMA)
    base.update(
        {
            "action_id": action_id,
            "action_type": "price_change",
            "title": "t",
            "rationale": "r",
            "customer_id": "C1",
            "item_id": "I1",
            "scope": "customer_item",
            "uom": "EA",
            "impact_basis": "gross_price_opportunity_fixed_volume",
            "expected_impact_cents": 1_000,
            "proposed_price_cents": 9_500,
            "current_price_cents": 9_000,
            "priority": "medium",
            "owner": "unassigned",
            "compliance_flags": "",
            "blocked": False,
            "scope_key": "price|C1|I1|EA|2026-01-15",
            "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        }
    )
    base.update(kw)
    return base


def _frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=ACTIONS_SCHEMA)


def test_duplicate_active_price_blocked_at_approval() -> None:
    actions = _frame(
        [
            _action("act_a"),
            _action("act_b"),  # same scope_key
        ]
    )
    statuses = pl.DataFrame(
        {"action_id": ["act_a", "act_b"], "status": ["approved", "under_review"]}
    )
    with pytest.raises(ReviewError, match="another active price action"):
        check_no_duplicate_active_price(
            actions.filter(pl.col("action_id") == "act_b").to_dicts()[0], actions, statuses
        )
    # A rejected competitor does not block.
    statuses2 = pl.DataFrame(
        {"action_id": ["act_a", "act_b"], "status": ["rejected", "under_review"]}
    )
    check_no_duplicate_active_price(
        actions.filter(pl.col("action_id") == "act_b").to_dicts()[0], actions, statuses2
    )
    # Different scope keys never collide.
    actions3 = _frame([_action("act_a"), _action("act_b", scope_key="price|C1|I1|EA|2026-02-15")])
    check_no_duplicate_active_price(
        actions3.filter(pl.col("action_id") == "act_b").to_dicts()[0], actions3, statuses
    )


def test_export_refuses_duplicate_scopes() -> None:
    actions = _frame([_action("act_a"), _action("act_b")])
    events = empty_events()
    now = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    for aid in ("act_a", "act_b"):
        for status in ("proposed", "under_review", "approved"):
            events = append_event(events, aid, who="u", at=now, to_status=status)
    with pytest.raises(ValueError, match="more than one"):
        build_price_file(actions, events, Rules().actions.price_export)


def test_portfolio_estimate_removes_double_counting() -> None:
    """A category pass-through and a pair price change on the same item: the
    portfolio excludes the priced pair from the pass-through recovery, so the
    joint estimate is strictly below the naive sum - and equals the
    hand-recomputed pieces."""
    panel = pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
                "segment_leaf": 1,
                "month_idx": 24300,
                "qty": 10.0,
                "price_cf": 9000.0,
                "supplier_id": "S1",
                "item_category": "CATA",
                "contract_id": None,
            },
            {
                "customer_id": "C2",
                "item_id": "I2",
                "segment_leaf": 1,
                "month_idx": 24300,
                "qty": 5.0,
                "price_cf": 9000.0,
                "supplier_id": "S1",
                "item_category": "CATA",
                "contract_id": None,
            },
        ]
    ).with_columns(pl.col("contract_id").cast(pl.String))
    guidance = pl.DataFrame(
        [
            {
                "customer_id": c,
                "item_id": i,
                "segment_leaf": 1,
                "guidance_status": "below_reference",
                "target_index": 0.95,
                "current_index": 0.90,
                "eff_list_cents": 10_000,
                "unit_cost_cents": 6_000,
                "invoice_floor_cents": 6_316,
                "band_p25": 0.92,
                "band_p75": 0.97,
                "contract_protected": False,
                "deduction_bp": 0,
                "fixed_deduction_cents": 0,
            }
            for c, i in (("C1", "I1"), ("C2", "I2"))
        ]
    )
    sens = pl.DataFrame(
        [
            {
                "segment": 1,
                "iv": -2.0,
                "iv_ci_low": -2.2,
                "iv_ci_high": -1.8,
                "status": "descriptive_elasticity",
                "support_log_lo": -0.5,
                "support_log_hi": 0.5,
            }
        ]
    )
    rules = Rules()
    actions = _frame(
        [
            _action(
                "act_price",
                expected_impact_cents=5_000,
                overlap_group="overlap|category:CATA",
            ),
            _action(
                "act_pt",
                action_type="cost_pass_through",
                customer_id=None,
                item_id=None,
                scope="category:CATA",
                scope_key="cost_pass_through|category:CATA",
                impact_basis="cost_recovery_at_chosen_level",
                expected_impact_cents=4_500,  # naive: recovery over I1 AND I2
                pass_through_level=0.5,
                proposed_price_cents=None,
                current_price_cents=None,
                overlap_group="overlap|category:CATA",
                scenario_id="scn|category:CATA",
            ),
        ]
    )
    groups = build_portfolio_groups(actions, panel, guidance, sens, rules)
    assert groups.height == 1
    g = groups.to_dicts()[0]
    assert g["n_actions"] == 2
    assert set(g["action_ids"].split(";")) == {"act_price", "act_pt"}
    assert g["naive_sum_cents"] == 9_500
    # Hand recompute: the pass-through recovery excluding C1/I1 equals the
    # grid's recovered amount on the I2-only panel at level 0.5.
    shock = CostShock("portfolio category:CATA", "category", "CATA", 0.10, dt.date(2000, 1, 1))
    residual = panel.filter(pl.col("item_id") != "I1")
    grid = pass_through_grid(residual, guidance, sens, shock, rules, steps=10)
    recovered = int(grid.filter((pl.col("pass_through") - 0.5).abs() < 1e-9)["recovered_cents"][0])
    assert g["portfolio_estimate_cents"] == 5_000 + recovered
    assert g["portfolio_estimate_cents"] < g["naive_sum_cents"]


def test_portfolio_dedupes_price_scopes() -> None:
    """Two active price actions on one scope count once in the portfolio."""
    actions = _frame(
        [
            _action("act_a", overlap_group="overlap|category:CATA", expected_impact_cents=5_000),
            _action("act_b", overlap_group="overlap|category:CATA", expected_impact_cents=7_000),
        ]
    )
    groups = build_portfolio_groups(actions, None, pl.DataFrame(), pl.DataFrame(), Rules())
    g = groups.to_dicts()[0]
    assert g["naive_sum_cents"] == 12_000
    assert g["portfolio_estimate_cents"] == 5_000  # deterministic first by id
