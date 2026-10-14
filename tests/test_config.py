"""Config schema validation tests (Phase 0)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from margin_engine.config import Rules, load_rules

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_rules_yaml_is_valid() -> None:
    rules = load_rules(REPO_ROOT / "config" / "rules.yaml")
    assert rules.schema_.version == 1
    assert rules.units_and_currency.reporting_currency == "USD"


def test_defaults_match_spec_section_8() -> None:
    rules = Rules()
    assert rules.outliers.zscore_threshold == 3.5
    assert rules.outliers.min_peer_group_size == 30
    assert rules.outliers.mad_zero_fallback_pct == 0.10
    assert rules.segmentation.commercial_tree.max_depth == 5
    assert rules.segmentation.margin_driver_tree.max_depth == 4
    assert rules.segmentation.commercial_tree.min_leaf_customers == 20
    assert rules.segmentation.commercial_tree.min_leaf_items == 10
    assert rules.price_guidance.min_pocket_margin == Decimal("0.05")
    assert rules.sensitivity.min_first_stage_f == 10.0
    assert rules.sensitivity.active_assortment_lookback_months == 6
    assert rules.opportunity.baseline_window_months == 12
    assert rules.tariff_scenarios.pass_through_levels == (0.0, 0.5, 1.0)


def test_min_pocket_margin_is_decimal_not_float() -> None:
    rules = Rules()
    assert isinstance(rules.price_guidance.min_pocket_margin, Decimal)


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        Rules.model_validate({"not_a_section": {}})


def test_unknown_nested_key_rejected() -> None:
    with pytest.raises(ValidationError):
        Rules.model_validate({"outliers": {"zscore_threshold": 3.5, "typo_key": 1}})


def test_invalid_value_rejected() -> None:
    with pytest.raises(ValidationError):
        Rules.model_validate({"outliers": {"zscore_threshold": -1.0}})


def test_abc_shares_must_sum_below_one() -> None:
    with pytest.raises(ValidationError):
        Rules.model_validate({"segmentation": {"abc_a_share": 0.9, "abc_b_share": 0.2}})


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(TypeError):
        load_rules(bad)


def test_empty_yaml_gives_defaults(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    rules = load_rules(empty)
    assert rules.outliers.zscore_threshold == 3.5
