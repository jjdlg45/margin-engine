"""Run 6, Phase 85.1: the reviewer's acceptance matrix, as a checked registry.

The independent review closed with an acceptance-test matrix: twenty-two
families a remediation has to cover. The tests exist, written phase by phase
across Runs 1-6 - what did not exist was the map from each family to the
tests that discharge it, in a form that breaks when someone renames or
deletes one.

This module is that map. Each family names the modules (and, for the
load-bearing ones, the specific tests) that implement it, and the test at the
bottom verifies every reference against the code itself: a module that stops
existing, or a named test that disappears, fails the matrix rather than
quietly thinning it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: family -> list of "module" or "module::test_name" references. Modules are
#: dotted paths under tests/.
ACCEPTANCE_MATRIX: dict[str, list[str]] = {
    "money and allocation": [
        "tests.test_allocation",
        "tests.property.test_reconciliation",
        "tests.test_waterfall_fixtures::test_normal_sale",
    ],
    "credits": [
        "tests.test_events::test_duplicate_credit_representations_counted_once",
        "tests.test_waterfall_fixtures",
    ],
    "rebate lifecycle": [
        "tests.test_rebate_subledger",
        "tests.test_tiered",
    ],
    "return accounting": [
        "tests.test_events",
        "tests.test_waterfall_fixtures::test_partial_return_in_ledger_not_observations",
    ],
    "freight": [
        "tests.test_charges_pools",
        "tests.test_charges_pools::test_billed_freight_never_enters_price_index",
    ],
    "margin objective": [
        "tests.test_opportunity",
        "tests.test_guidance",
    ],
    "price deductions": [
        "tests.test_deductions",
    ],
    "floor": [
        "tests.test_floors_bands",
        "tests.test_guidance::test_pocket_floor_spec_example",
    ],
    "segment leakage": [
        "tests.test_segment_leakage",
        "tests.test_segment_leakage::test_an_innocent_name_does_not_get_a_feature_past_the_guard",
        "tests.test_segment_leakage::test_the_shortcut_really_does_destroy_the_partition",
    ],
    "IV exclusion": [
        "tests.test_competitor_path",
        "tests.test_sensitivity::test_invalid_instrument_overid_rejects",
    ],
    "IV zeros": [
        "tests.test_total_quantity",
        "tests.test_zero_row_prices",
    ],
    "cluster structure": [
        "tests.test_clustering",
    ],
    "weak IV": [
        "tests.test_weak_iv",
        "tests.test_sensitivity::test_weak_instrument_never_causal",
    ],
    "price support": [
        "tests.test_price_support",
    ],
    "results tracker": [
        "tests.test_tracker_design",
        "tests.test_tracking",
        "tests.test_tracker_design::test_a_pair_that_stops_buying_is_measured_at_zero_not_dropped",
    ],
    "action overlap": [
        "tests.test_action_overlap",
    ],
    "stale action": [
        "tests.test_stale_actions",
    ],
    "maker-checker": [
        "tests.test_actions",
        "tests.test_production_mode::test_self_approval_is_refused_on_the_server_not_in_the_browser",
    ],
    "export recovery": [
        "tests.test_price_export",
    ],
    "UCI real-data scope": [
        "tests.test_online_retail_sample",
    ],
    "scale parity": [
        "tests.test_spill",
        "tests.test_spill::test_every_export_is_byte_identical_across_the_two_paths",
    ],
    "spreadsheet edge cases": [
        "tests.test_edge_cases",
        "tests.test_edge_cases::test_a_formula_with_no_computed_value_fails_actionably",
        "tests.test_edge_cases::test_hidden_rows_are_kept_and_counted",
        "tests.test_edge_cases::test_a_date_serial_is_named_as_one",
        "tests.test_input_formats::test_per_table_delimiter_override",
    ],
    "mapping version in outputs": [
        "tests.test_edge_cases",
        "tests.test_stale_actions",
        "tests.test_api::test_health_and_version",
    ],
    "action scope spans": [
        "tests.test_edge_cases",
        "tests.test_actions",
    ],
    "parent/child contracts": [
        "tests.test_edge_cases",
        "tests.test_edge_cases::test_a_parent_contract_protects_its_children",
        "tests.test_hierarchy",
    ],
    "FX translation policy": [
        "tests.test_edge_cases::test_a_converted_ledger_states_the_translation_policy",
        "tests.test_fx",
    ],
    "determinism scope": [
        "tests.test_webexport::test_bundle_byte_identical_across_processes_and_thread_counts",
        "tests.test_full_run",
        "tests.test_generator::test_same_seed_identical_output",
    ],
}


def _resolve(ref: str) -> str | None:
    """None when the reference resolves; otherwise what is wrong with it."""
    module_name, _, test_name = ref.partition("::")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        return f"{ref}: module does not import ({exc})"
    if test_name and not hasattr(module, test_name):
        return f"{ref}: test function no longer exists"
    return None


def test_every_family_resolves_to_living_tests() -> None:
    problems = [p for refs in ACCEPTANCE_MATRIX.values() for ref in refs if (p := _resolve(ref))]
    assert not problems, "\n".join(problems)


def test_the_matrix_covers_every_named_family() -> None:
    """The 22 are the reviewer's and the 5 are Phase 85.2's; a family dropped
    from this dict is the matrix quietly shrinking, which is the failure mode
    this module exists to prevent."""
    # 22 from the review, plus the five Phase 85.2 edge-case families.
    assert len(ACCEPTANCE_MATRIX) == 27
    assert all(refs for refs in ACCEPTANCE_MATRIX.values()), "a family with no tests is a claim"


@pytest.mark.parametrize("family", sorted(ACCEPTANCE_MATRIX))
def test_family_has_at_least_one_module_level_suite(family: str) -> None:
    """Every family rests on at least one whole module, not only a cherry-
    picked test: a single named test proves a point, a module carries the
    edge cases around it."""
    assert any("::" not in ref for ref in ACCEPTANCE_MATRIX[family]), family
