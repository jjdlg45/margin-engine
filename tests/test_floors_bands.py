"""Phase 67 (Run 6): floors and bands guardrails — independence counts, stale
lists, mixed units, scoped contribution floors, authorized exceptions, and
ERP price increments."""

from __future__ import annotations

import datetime as dt
from datetime import date

import polars as pl

from margin_engine.actions.generate import _below_floor_actions
from margin_engine.config import Rules
from margin_engine.guidance.bands import build_guidance
from margin_engine.opportunity.sizing import optimize_targets, price_opportunities


def _frame(rows: list[dict]) -> pl.DataFrame:
    base = {
        "customer_id": "C1",
        "item_id": "I1",
        "segment_leaf": 1,
        "price_index": 0.90,
        "invoice_date": date(2024, 6, 15),
        "quantity_norm": 1.0,
        "product_cost_cents": 5_000,
        "eff_list_cents": 10_000,
        "contract_id": None,
        "flag_data_quality_anomaly": False,
    }
    return pl.DataFrame([{**base, **r} for r in rows]).with_columns(
        pl.col("contract_id").cast(pl.String), pl.col("invoice_date").cast(pl.Date)
    )


def _many(n_customers: int = 6, n_per: int = 6) -> list[dict]:
    return [
        {"customer_id": f"C{c}", "item_id": f"I{i}", "price_index": 0.88 + 0.01 * (i % 5)}
        for c in range(n_customers)
        for i in range(n_per)
    ]


def test_bands_report_independence_counts() -> None:
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 1}})
    res = build_guidance(_frame(_many()), rules, None)
    band = res.bands.to_dicts()[0]
    assert band["n_band_customers"] == 6
    assert band["n_band_pairs"] == 36
    assert band["n_band_months"] == 1
    assert band["n_band_list_regimes"] >= 1


def test_single_customer_band_is_not_a_market() -> None:
    """36 observations from ONE customer: plenty of rows, no independence -
    the band cannot be a market reference (min_band_customers)."""
    rows = [{"customer_id": "C1", "item_id": f"I{i}"} for i in range(36)]
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 1}})
    res = build_guidance(_frame(rows), rules, None)
    g = res.guidance
    assert (g["guidance_status"] == "insufficient_data").all()
    assert (g["target_unavailable_reason"] == "thin_segment_or_data").all()


def test_stale_list_blocks_target_with_reason() -> None:
    """A list in force but older than the maximum age at the pair's last sale:
    dollar floors stay, but no target is produced - a target against a stale
    list is a number nobody can act on."""
    rows = _many()
    rows.append(
        {
            "customer_id": "CX",
            "item_id": "IX",
            "list_effective_date": date(2021, 1, 1),  # > 24 months before the sale
        }
    )
    for r in rows:
        r.setdefault("list_effective_date", date(2024, 1, 1))
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 1}})
    res = build_guidance(_frame(rows), rules, None)
    cx = res.guidance.filter(pl.col("customer_id") == "CX").to_dicts()[0]
    assert cx["list_provenance"] == "stale_list"
    assert cx["target_index"] is None
    assert cx["start_index"] is None
    assert cx["target_unavailable_reason"] == "stale_list"
    assert cx["invoice_floor_cents"] is not None  # dollar floors are kept
    fresh = res.guidance.filter(pl.col("customer_id") == "C1").to_dicts()[0]
    assert fresh["target_index"] is not None


def test_mixed_units_need_an_equivalence_map() -> None:
    """The same item code trading in two normalized units has no single
    comparable price: excluded from the bands, pairs labeled with the reason."""
    rows = _many()
    rows += [
        {"customer_id": "C1", "item_id": "I9", "uom_norm": "EA"},
        {"customer_id": "C2", "item_id": "I9", "uom_norm": "CS", "price_index": 12.0},
    ]
    for r in rows:
        r.setdefault("uom_norm", "EA")
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 1}})
    res = build_guidance(_frame(rows), rules, None)
    i9 = res.guidance.filter(pl.col("item_id") == "I9")
    assert (i9["guidance_status"] == "insufficient_data").all()
    assert (i9["target_unavailable_reason"] == "mixed_units_no_equivalence").all()
    # The wild 12.0 index never contaminated the band.
    assert res.bands.to_dicts()[0]["band_p75"] < 1.0
    assert any("equivalence" in n for n in res.notes)


def _k_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "customer_id": r["customer_id"],
                "item_id": r["item_id"],
                "cts_per_unit_cents": r.get("k", 0),
            }
            for r in rows
        ]
    )


def test_contribution_floor_scoped_to_roles() -> None:
    rows = _many()
    rows += [
        {"customer_id": "CK", "item_id": "IK", "item_role": "kvi"},
        {"customer_id": "CN", "item_id": "IN", "item_role": "core"},
    ]
    for r in rows:
        r.setdefault("item_role", "core")
    rules = Rules.model_validate(
        {
            "price_guidance": {
                "min_band_observations": 1,
                "min_contribution_margin": "0.10",
                "contribution_floor_item_roles": ["kvi"],
            }
        }
    )
    k = _k_frame(
        [{"customer_id": "CK", "item_id": "IK", "k": 2_000}, {"customer_id": "CN", "item_id": "IN", "k": 2_000}]
    )
    res = build_guidance(_frame(rows), rules, None, cts_per_unit=k)
    ck = res.guidance.filter(pl.col("customer_id") == "CK").to_dicts()[0]
    cn = res.guidance.filter(pl.col("customer_id") == "CN").to_dicts()[0]
    # In scope: contribution floor computed, binds, K assumption disclosed.
    assert ck["invoice_contribution_floor_cents"] is not None
    assert ck["binding_floor_basis"] == "contribution_margin"
    assert "estimated from configured" in ck["k_assumption"]
    assert "2000" in ck["k_assumption"]
    # Out of scope: gross floor only; no borrowed contribution policy.
    assert cn["invoice_contribution_floor_cents"] is None
    assert cn["binding_floor_basis"] == "pocket_gross_margin"
    assert cn["k_assumption"] is None


def test_authorized_below_floor_role_blocks_fix_action() -> None:
    """An explicitly authorized below-floor role (loss leader) keeps its
    visible status, and the fix action generates BLOCKED with the
    authorization shown - the config is the only override path."""
    base = pl.DataFrame(
        {
            "customer_id": ["C1", "C2"],
            "item_id": ["I1", "I2"],
            "segment_leaf": [1, 1],
            "guidance_status": ["below_floor", "below_floor"],
            "evidence_status": [None, None],
            "volume_treatment": [None, None],
            "binding_floor_basis": ["pocket_gross_margin"] * 2,
            "contract_protected": [False, False],
            "below_floor_authorized": [True, False],
            "uom": ["EA", "EA"],
            "baseline_qty": [10.0, 10.0],
            "current_invoice_price_cents": [5_000.0, 5_000.0],
            "pocket_retention": [1.0, 1.0],
            "_floor_price_cents": [6_316, 6_316],
            "_compliance": ["", ""],
            "_seg_elasticity": [None, None],
        }
    )
    out = _below_floor_actions(base, dt.date(2026, 1, 1))
    rows = {r["customer_id"]: r for r in out.to_dicts()}
    assert rows["C1"]["blocked"] is True
    assert "authorized by policy" in rows["C1"]["blocked_reason"]
    assert rows["C2"]["blocked"] is False


def test_price_increment_snapping_never_breaches_floor() -> None:
    """Optimized prices snap to the configured ERP increment; the floor is
    rounded UP to the increment so a snap can never cross it."""
    rules = Rules.model_validate({"price_guidance": {"price_increment_cents": 300}})
    guidance = pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
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
        ]
    )
    panel = pl.DataFrame(
        [
            {
                "customer_id": "C1",
                "item_id": "I1",
                "segment_leaf": 1,
                "month_idx": 24300,
                "qty": 20.0,
                "price_cf": 9000.0,
                "contract_id": None,
            }
        ]
    ).with_columns(pl.col("contract_id").cast(pl.String))
    sens = pl.DataFrame(
        [
            {
                "segment": 1,
                "iv": -2.0,
                "iv_ci_low": -2.2,
                "iv_ci_high": -1.8,
                "status": "causal_elasticity_supported",
                "support_log_lo": -0.5,
                "support_log_hi": 0.5,
            }
        ]
    )
    opp = price_opportunities(guidance, panel, sens, rules)
    opp = opp.with_columns(pl.lit(-2.0).alias("elasticity"))
    out = optimize_targets(opp, guidance, rules)
    row = out.to_dicts()[0]
    # Unsnapped optimum clips to the corridor top 9,700; snapped to the
    # 300-cent increment: 9,600 (still above the increment-rounded floor 6,600).
    assert row["optimal_price_cents"] == 9_600
    assert row["optimal_price_cents"] % 300 == 0
