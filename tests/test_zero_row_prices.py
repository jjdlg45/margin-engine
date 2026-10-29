"""Phase 72 (Run 6): the zero-row price ladder.

A zero row still needs a price, and which price it gets decides what the
extensive margin can identify. A carried-forward price has no within-pair
variation during a zero run, so it is the last resort rather than the only
option. These tests pin the ladder's order, its labeling, and the fact that it
degrades to the previous behaviour when no better source exists.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from margin_engine.config import load_rules
from margin_engine.panel.build import build_panel

RULES = load_rules.__wrapped__ if hasattr(load_rules, "__wrapped__") else load_rules


def _rules():
    from pathlib import Path

    return load_rules(Path(__file__).resolve().parents[1] / "config" / "rules.yaml")


def _observations() -> pl.DataFrame:
    """One pair buying in months 1 and 5, so months 2-4 are zero rows."""
    rows = []
    for m, qty, net in ((1, 10.0, 10_000), (5, 10.0, 9_000)):
        rows.append(
            {
                "source_line_key": f"L{m}",
                "customer_id": "C1",
                "item_id": "I1",
                "invoice_date": dt.date(2024, m, 15),
                "quantity_norm": qty,
                "net_amount_cents": net,
                "segment_leaf": 1,
                "contract_id": None,
            }
        )
    # A second pair keeps the item "available" in every month.
    for m in range(1, 7):
        rows.append(
            {
                "source_line_key": f"K{m}",
                "customer_id": "C2",
                "item_id": "I1",
                "invoice_date": dt.date(2024, m, 10),
                "quantity_norm": 5.0,
                "net_amount_cents": 5_000,
                "segment_leaf": 1,
                "contract_id": None,
            }
        )
    return pl.DataFrame(rows)


def _price_list() -> pl.DataFrame:
    """List price steps up in month 3, so the list rung carries real variation
    during the zero run while a carried-forward price would stay flat."""
    return pl.DataFrame(
        {
            "item_id": ["I1", "I1"],
            "effective_date": [dt.date(2024, 1, 1), dt.date(2024, 3, 1)],
            "list_price_scaled": [125_000.0, 150_000.0],
        }
    )


def _quotes() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "customer_id": ["C1"],
            "item_id": ["I1"],
            "quote_date": [dt.date(2024, 4, 20)],
            "quoted_price_cents": [1_234.0],
            "outcome": ["L"],
        }
    )


def _panel(**kw) -> pl.DataFrame:
    return build_panel(_observations(), None, None, None, _rules(), **kw)


def test_without_better_sources_the_ladder_is_the_old_behaviour() -> None:
    p = _panel()
    zeros = p.filter((pl.col("customer_id") == "C1") & (pl.col("qty") == 0)).sort("month_idx")
    # Months 2-4 are zero rows between the two purchases, and month 6 is a zero
    # row that is still in active assortment after the month-5 purchase.
    assert zeros.height == 4
    assert zeros["price_status"].to_list() == ["carried_forward"] * 4
    assert zeros["price_carried_forward"].all()
    # Flat at the last observed price within each run - which is exactly the
    # problem the other rungs address: months 2-4 all carry month 1's 1000.
    assert zeros["price_cf"].to_list() == [1_000.0, 1_000.0, 1_000.0, 900.0]


LIST_LADDER = ("quoted", "list_x_pair_discount", "carried_forward")


def test_purchases_are_labeled_observed_not_imputed() -> None:
    p = _panel(price_list=_price_list(), zero_price_sources=LIST_LADDER)
    bought = p.filter((pl.col("customer_id") == "C1") & (pl.col("qty") > 0))
    assert bought["price_status"].to_list() == ["observed", "observed"]
    assert not bought["price_carried_forward"].any()


def test_list_rung_carries_the_list_move_into_the_zero_run() -> None:
    """The list rung is OPT-IN: measurement showed it adds little within-pair
    variation, makes the total-quantity estimate worse, and widens the
    imputed-price robustness gap, so the default ladder leaves it out. It is
    asked for explicitly here because this test is about the rung itself."""
    p = _panel(price_list=_price_list(), zero_price_sources=LIST_LADDER)
    zeros = p.filter((pl.col("customer_id") == "C1") & (pl.col("qty") == 0)).sort("month_idx")
    assert zeros["price_status"].to_list() == ["list_x_pair_discount"] * 4
    assert not zeros["price_carried_forward"].any()
    prices = zeros["price_cf"].to_list()
    # Month 1 bought at 1000 against a list of 1250, an index of 0.8. The list
    # steps to 1500 in month 3, so the imputed price steps with it - the
    # variation a carried-forward price does not have.
    assert prices[:3] == [1_000.0, 1_200.0, 1_200.0]
    # Month 5 bought at 900 against a list of 1500, an index of 0.6, so the
    # month-6 zero row reprices at the discount the pair last negotiated.
    assert prices[3] == 900.0


def test_a_lost_quote_outranks_both_imputations() -> None:
    p = _panel(price_list=_price_list(), quotes=_quotes(), zero_price_sources=LIST_LADDER)
    row = p.filter(
        (pl.col("customer_id") == "C1") & (pl.col("qty") == 0) & (pl.col("month_idx") % 12 == 4)
    )
    assert row.height == 1
    assert row["price_status"][0] == "quoted"
    assert row["price_cf"][0] == 1_234.0
    # The months with no quote still use the list rung.
    others = p.filter(
        (pl.col("customer_id") == "C1") & (pl.col("qty") == 0) & (pl.col("month_idx") % 12 != 4)
    )
    assert others["price_status"].unique().to_list() == ["list_x_pair_discount"]


def test_a_won_quote_is_not_a_price_for_a_non_purchase() -> None:
    won = _quotes().with_columns(pl.lit("W").alias("outcome"))
    p = _panel(price_list=_price_list(), quotes=won, zero_price_sources=LIST_LADDER)
    zeros = p.filter((pl.col("customer_id") == "C1") & (pl.col("qty") == 0))
    assert "quoted" not in zeros["price_status"].to_list()


def test_the_ladder_order_is_honoured() -> None:
    """Asking for carried-forward only must not silently use the list rung -
    the robustness variants in this phase depend on the order being obeyed."""
    p = _panel(
        price_list=_price_list(),
        quotes=_quotes(),
        zero_price_sources=("carried_forward",),
    )
    zeros = p.filter((pl.col("customer_id") == "C1") & (pl.col("qty") == 0))
    assert zeros["price_status"].unique().to_list() == ["carried_forward"]


def test_the_default_ladder_leaves_the_list_rung_out() -> None:
    """Pinned deliberately: the default is `quoted` then `carried_forward`.
    Measurement put the list imputation out of the default, so a change back
    should have to change this test and say why."""
    import inspect

    from margin_engine.panel.build import build_panel as _bp

    default = inspect.signature(_bp).parameters["zero_price_sources"].default
    assert default == ("quoted", "carried_forward")
    # And with a price list present it is still not used unless asked for.
    p = _panel(price_list=_price_list())
    zeros = p.filter((pl.col("customer_id") == "C1") & (pl.col("qty") == 0))
    assert "list_x_pair_discount" not in zeros["price_status"].to_list()
