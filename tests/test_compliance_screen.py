"""Run 6, Phase 79 (Finding 17): what the Robinson-Patman flag is allowed to say.

Same industry and region is what the data can do on its own. It is not a
finding that two customers compete, a missing attribute is not evidence that
they do not, and a price difference is the start of the question rather than
the answer. These tests hold the output to that.
"""

from __future__ import annotations

import polars as pl
import pytest

from margin_engine.compliance import (
    BASIS_REVIEWED,
    BASIS_SCREENED,
    SCREEN_ATTRIBUTES_MISSING,
    SCREEN_OK,
    run_robinson_patman,
    screen_customers,
)
from margin_engine.config import Rules

FORBIDDEN = ("violation", "compliant", "illegal", "unlawful", "guaranteed")


def _rules(**rp: object) -> Rules:
    return Rules.model_validate({"compliance": {"robinson_patman": rp}}) if rp else Rules()


def _guidance(prices: dict[str, int], item: str = "I1", **extra: object) -> pl.DataFrame:
    rows = []
    for cust, cents in prices.items():
        row: dict[str, object] = {
            "customer_id": cust,
            "item_id": item,
            "target_index": cents / 10_000,
            "current_index": cents / 10_000 * 1.02,
            "eff_list_cents": 10_000,
            "deduction_bp": 0,
            "fixed_deduction_cents": 0,
            "total_qty": 100.0,
            "channel": "direct",
            "item_role": "standard",
            "uom_basis": "EA",
        }
        row.update(extra)
        rows.append(row)
    return pl.DataFrame(rows)


def _customers(**attrs: tuple[str | None, str | None]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"customer_id": c, "industry": ind, "region": reg} for c, (ind, reg) in attrs.items()]
    )


# ---------------------------------------------------------------------------
# the screen is a screen
# ---------------------------------------------------------------------------


def test_a_customer_missing_an_attribute_is_unscreened_not_cleared() -> None:
    """Before Phase 79 it dropped out of the join and nobody heard about it."""
    customers = _customers(A=("mro", "north"), B=(None, "north"), C=("mro", None))
    screen = screen_customers(customers, ["industry", "region"])
    by_id = dict(zip(screen["customer_id"], screen["screen_status"], strict=True))
    assert by_id == {"A": SCREEN_OK, "B": SCREEN_ATTRIBUTES_MISSING, "C": SCREEN_ATTRIBUTES_MISSING}
    missing = dict(zip(screen["customer_id"], screen["missing_attributes"], strict=True))
    assert missing["B"] == "industry"
    assert missing["C"] == "region"


def test_an_empty_string_is_missing_too() -> None:
    screen = screen_customers(_customers(A=("", "north")), ["industry", "region"])
    assert screen["screen_status"][0] == SCREEN_ATTRIBUTES_MISSING


def test_the_run_reports_who_could_not_be_screened() -> None:
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000, "C": 30_000}),
        _customers(A=("mro", "north"), B=("mro", "north"), C=(None, None)),
        None,
        _rules(),
    )
    note = next(n for n in res.notes if "could not" in n)
    assert "C" in note
    assert "not screened is not the same as not competing" in note.lower()
    assert set(res.screen["screen_status"].to_list()) == {SCREEN_OK, SCREEN_ATTRIBUTES_MISSING}


def test_two_customers_with_nothing_on_file_are_not_thereby_alike() -> None:
    """Null is not a value they share."""
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=(None, None), B=(None, None)),
        None,
        _rules(),
    )
    assert res.detail.height == 0


# ---------------------------------------------------------------------------
# the reviewed map overrides the screen in both directions
# ---------------------------------------------------------------------------


def _map(*rows: tuple[str, str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"customer_id_a": a, "customer_id_b": b, "relationship": r} for a, b, r in rows]
    )


def test_a_reviewer_can_clear_a_screened_pair() -> None:
    args = (
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=("mro", "north"), B=("mro", "north")),
        None,
        _rules(),
    )
    assert run_robinson_patman(*args).detail.height == 1
    cleared = run_robinson_patman(*args, competitor_map=_map(("A", "B", "not_competitor")))
    assert cleared.detail.height == 0
    assert any("cleared by a reviewer" in n for n in cleared.notes)


def test_a_confirmed_pair_says_so_and_a_screened_one_says_so() -> None:
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=("mro", "north"), B=("mro", "north")),
        None,
        _rules(),
        competitor_map=_map(("A", "B", "competitor")),
    )
    assert res.detail["pair_basis"][0] == BASIS_REVIEWED

    screened = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=("mro", "north"), B=("mro", "north")),
        None,
        _rules(),
    )
    assert screened.detail["pair_basis"][0] == BASIS_SCREENED
    assert any("screened candidate" in n for n in screened.notes)


def test_the_map_is_direction_free() -> None:
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=("mro", "north"), B=("mro", "north")),
        None,
        _rules(),
        competitor_map=_map(("B", "A", "not_competitor")),
    )
    assert res.detail.height == 0


def test_requiring_a_reviewed_map_without_one_flags_nothing_and_says_why() -> None:
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=("mro", "north"), B=("mro", "north")),
        None,
        _rules(require_reviewed_map=True),
    )
    assert res.detail.height == 0
    assert any("no customer_competitors table was mapped" in n for n in res.notes)


def test_requiring_a_reviewed_map_drops_unconfirmed_candidates() -> None:
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000, "C": 30_000}),
        _customers(A=("mro", "north"), B=("mro", "north"), C=("mro", "north")),
        None,
        _rules(require_reviewed_map=True),
        competitor_map=_map(("A", "B", "competitor")),
    )
    assert set(zip(res.detail["customer_id_a"], res.detail["customer_id_b"], strict=True)) == {
        ("A", "B")
    }


# ---------------------------------------------------------------------------
# what a flagged pair has to show
# ---------------------------------------------------------------------------


def test_a_flag_carries_the_facts_a_reviewer_needs() -> None:
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=("mro", "north"), B=("mro", "north")),
        pl.DataFrame(
            {
                "customer_id": ["A", "B"],
                "item_id": ["I1", "I1"],
                "cts_per_unit_cents": [100, 400],
            }
        ),
        _rules(),
    )
    row = res.detail.to_dicts()[0]
    for field in (
        "item_role",
        "uom_basis",
        "current_price_a_cents",
        "recommended_net_a_cents",
        "quantity_a",
        "channel_a",
        "deduction_bp_a",
        "cts_per_unit_a_cents",
        "data_coverage",
        "pair_basis",
    ):
        assert field in row, field
    # Cost to serve is supporting evidence: present, and clearly a difference.
    assert row["cts_difference_cents"] == -300
    assert "cost to serve" in row["data_coverage"]


def test_the_net_basis_is_compared_as_well_as_the_invoice_basis() -> None:
    """One buyer on a 10% rebate and one on none are further apart than their
    invoice prices say, and the row shows both numbers."""
    g = pl.concat(
        [
            _guidance({"A": 10_000}, deduction_bp=0),
            _guidance({"B": 11_000}, deduction_bp=1_000),
        ]
    )
    res = run_robinson_patman(g, _customers(A=("mro", "north"), B=("mro", "north")), None, _rules())
    row = res.detail.to_dicts()[0]
    assert row["price_difference_pct"] == pytest.approx(10.0, abs=0.01)
    # B keeps 9,900 of its 11,000, so on a net basis A is now the dearer of
    # the two and the gap nearly closes: (10,000 - 9,900) / 9,900 = 1.01%.
    assert row["recommended_net_b_cents"] == 9_900
    assert row["net_difference_pct"] == pytest.approx(1.0101, abs=0.001)


def test_data_coverage_names_what_is_missing() -> None:
    thin = pl.DataFrame(
        {
            "customer_id": ["A", "B"],
            "item_id": ["I1", "I1"],
            "target_index": [1.0, 2.0],
            "eff_list_cents": [10_000, 10_000],
        }
    )
    res = run_robinson_patman(
        thin, _customers(A=("mro", "north"), B=("mro", "north")), None, _rules()
    )
    assert res.detail.height == 1
    assert res.detail["data_coverage"][0] == "none"


def test_absent_reviewer_evidence_is_absent_not_clear() -> None:
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=("mro", "north"), B=("mro", "north")),
        None,
        _rules(),
    )
    row = res.detail.to_dicts()[0]
    assert row["meeting_competition_record"] is None
    assert row["reviewer_decision"] is None
    assert any("Absent, not clear." in n for n in res.notes)


def test_recorded_evidence_reaches_the_row() -> None:
    evidence = pl.DataFrame(
        {
            "customer_id_a": ["A"],
            "customer_id_b": ["B"],
            "meeting_competition_record": ["matched a written bid from a rival, 2024-03-02"],
            "contemporaneous_quote": ["Q-1182"],
            "reviewer_decision": ["accepted, meeting competition in good faith"],
            "reviewer": ["j.diaz"],
            "reviewed_date": ["2024-03-10"],
        }
    )
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000}),
        _customers(A=("mro", "north"), B=("mro", "north")),
        None,
        _rules(),
        evidence=evidence,
    )
    row = res.detail.to_dicts()[0]
    assert row["reviewer"] == "j.diaz"
    assert "good faith" in row["reviewer_decision"]


# ---------------------------------------------------------------------------
# what the output may never say
# ---------------------------------------------------------------------------


def test_no_output_calls_anything_a_violation_or_compliant() -> None:
    res = run_robinson_patman(
        _guidance({"A": 10_000, "B": 20_000, "C": 30_000}),
        _customers(A=("mro", "north"), B=("mro", "north"), C=(None, None)),
        None,
        _rules(),
        competitor_map=_map(("A", "B", "competitor")),
    )
    text = " ".join(res.notes).lower()
    text += " ".join(str(v) for v in res.detail.to_dicts()[0].values()).lower()
    text += " ".join(res.detail.columns).lower()
    for word in FORBIDDEN:
        assert word not in text, f"the review flag used the word {word!r}"
    assert "review" in text
