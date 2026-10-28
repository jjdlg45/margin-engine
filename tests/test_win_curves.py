"""Run 6, Phase 77 (Finding 21): what counts as evidence in a win/loss model.

The Phase 16 implementation read `outcome == "W"` as a win and everything
else as a loss, treated every revision of a negotiation as an independent
draw, and judged its own precision from standard errors that assumed quotes
to one customer were independent. Each of those is tested here against a
world whose win curve is known exactly, so "the fix matters" is a measured
difference rather than an argument.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from margin_engine.config import Rules
from margin_engine.extended.quotes import (
    CALIBRATION_OFF,
    CALIBRATION_OK,
    CALIBRATION_UNTESTED,
    OUTCOME_EXPIRED,
    OUTCOME_LOST,
    OUTCOME_OPEN,
    OUTCOME_UNRECOGNIZED,
    OUTCOME_WITHDRAWN,
    OUTCOME_WON,
    STATUS_ESTIMATED,
    STATUS_LOW_PRECISION,
    classify_outcomes,
    collapse_revisions,
    fit_win_curves,
    prepare_quotes,
)

# The planted curve: P(win) = sigmoid(A + B x price index), 50% at 0.95.
PLANTED_B = -8.0
PLANTED_A = -PLANTED_B * 0.95


def _rules(**sensitivity: object) -> Rules:
    return Rules.model_validate({"sensitivity": sensitivity}) if sensitivity else Rules()


def _segments(quotes: pl.DataFrame) -> pl.DataFrame:
    return (
        quotes.select("customer_id", "item_id")
        .unique()
        .with_columns(pl.lit(1, dtype=pl.Int64).alias("segment_leaf"))
    )


def _decided_quotes(n: int = 3000, seed: int = 0, *, n_customers: int = 300) -> pl.DataFrame:
    """Quotes whose outcomes come from the planted curve and nowhere else."""
    rng = np.random.default_rng(seed)
    index = rng.uniform(0.75, 1.15, n)
    p = 1.0 / (1.0 + np.exp(-(PLANTED_A + PLANTED_B * index)))
    won = rng.random(n) < p
    start = dt.date(2024, 1, 1)
    return pl.DataFrame(
        {
            "quote_date": [
                start + dt.timedelta(days=int(d)) for d in np.sort(rng.integers(0, 720, n))
            ],
            "customer_id": [f"C{i % n_customers:03d}" for i in range(n)],
            "item_id": [f"I{i % 40:03d}" for i in range(n)],
            "quantity": rng.integers(1, 40, n).astype(float),
            "quote_index": index,
            "quoted_price_cents": (index * 10_000).astype(np.int64),
            "close_price_cents": pl.Series(
                [int(i * 9_700) if w else None for i, w in zip(index, won, strict=True)],
                dtype=pl.Int64,
            ),
            "outcome": np.where(won, "W", "L"),
        }
    )


def _fit(quotes: pl.DataFrame, rules: Rules | None = None) -> tuple[dict, object]:
    rules = rules or _rules()
    curves, intake = fit_win_curves(quotes, _segments(quotes), rules)
    return curves.to_dicts()[0], intake


# ---------------------------------------------------------------------------
# the outcome taxonomy
# ---------------------------------------------------------------------------


def test_outcome_codes_map_to_classes_case_and_space_insensitively() -> None:
    q = pl.DataFrame({"outcome": [" w ", "Lost", "OPEN", "x", "expired", "banana", None]})
    got = classify_outcomes(q, _rules())["outcome_class"].to_list()
    assert got == [
        OUTCOME_WON,
        OUTCOME_LOST,
        OUTCOME_OPEN,
        OUTCOME_WITHDRAWN,
        OUTCOME_EXPIRED,
        OUTCOME_UNRECOGNIZED,
        OUTCOME_UNRECOGNIZED,
    ]


def test_an_unknown_code_is_excluded_and_named_not_read_as_a_loss() -> None:
    q = _decided_quotes(n=600)
    q = q.with_columns(
        pl.when(pl.int_range(pl.len()) < 50)
        .then(pl.lit("PENDING_REVIEW"))
        .otherwise(pl.col("outcome"))
        .alias("outcome")
    )
    _, intake = prepare_quotes(q, _segments(q), _rules())
    assert intake.excluded[OUTCOME_UNRECOGNIZED] == 50
    assert any("PENDING_REVIEW" in n for n in intake.notes)


def test_every_row_is_accounted_for() -> None:
    """Modeled plus excluded equals what came in. The same discipline the
    ledger is held to, applied to the quotes table."""
    q = _decided_quotes(n=900)
    q = q.with_columns(
        pl.when(pl.int_range(pl.len()) < 100)
        .then(pl.lit("O"))
        .when(pl.int_range(pl.len()) < 160)
        .then(pl.lit("X"))
        .otherwise(pl.col("outcome"))
        .alias("outcome")
    )
    _, intake = prepare_quotes(q, _segments(q), _rules())
    assert intake.n_modeled + sum(intake.excluded.values()) == intake.n_rows


def test_a_missing_outcome_column_switches_the_module_off() -> None:
    q = _decided_quotes(n=100).drop("outcome")
    model, intake = prepare_quotes(q, _segments(q), _rules())
    assert model.height == 0
    assert any("no outcome column" in n for n in intake.notes)


# ---------------------------------------------------------------------------
# the bias the old rule caused
# ---------------------------------------------------------------------------


def _with_undecided(q: pl.DataFrame, seed: int = 7, n: int = 900) -> pl.DataFrame:
    """Add quotes that were never decided, at prices that were never tested.

    Withdrawn and expired quotes cluster at LOW indexes here - the customer's
    project fell through, which has nothing to do with the price being too
    high. That is the shape that makes the old rule wrong in a measurable
    direction: counted as losses, cheap quotes appear to lose, and the fitted
    curve flattens.
    """
    rng = np.random.default_rng(seed)
    index = rng.uniform(0.75, 0.85, n)
    start = dt.date(2024, 1, 1)
    extra = pl.DataFrame(
        {
            "quote_date": [start + dt.timedelta(days=int(d)) for d in rng.integers(0, 720, n)],
            "customer_id": [f"C{i % 300:03d}" for i in range(n)],
            "item_id": [f"I{i % 40:03d}" for i in range(n)],
            "quantity": rng.integers(1, 40, n).astype(float),
            "quote_index": index,
            "quoted_price_cents": (index * 10_000).astype(np.int64),
            "close_price_cents": pl.Series([None] * n, dtype=pl.Int64),
            "outcome": rng.choice(["X", "E", "O"], n),
        }
    )
    return pl.concat([q, extra], how="vertical_relaxed").sort("quote_date")


def test_undecided_quotes_are_excluded_and_the_planted_curve_is_recovered() -> None:
    q = _with_undecided(_decided_quotes())
    curve, intake = _fit(q)
    assert intake.excluded[OUTCOME_WITHDRAWN] > 0
    assert intake.excluded[OUTCOME_EXPIRED] > 0
    assert intake.excluded[OUTCOME_OPEN] > 0
    assert curve["status"] == STATUS_ESTIMATED, curve["reasons"]
    rel = abs(curve["price_coef"] - PLANTED_B) / abs(PLANTED_B)
    assert rel <= 0.15, f"recovered {curve['price_coef']:.2f} against planted {PLANTED_B}"


def test_reading_every_non_win_as_a_loss_flattens_the_curve() -> None:
    """The Phase 16 rule, reproduced: it does not merely add noise, it moves
    the coefficient toward zero, which understates how much price matters."""
    q = _with_undecided(_decided_quotes())
    honest, _ = _fit(q)
    old_rule = q.with_columns(
        pl.when(pl.col("outcome") == "W").then(pl.lit("W")).otherwise(pl.lit("L")).alias("outcome")
    )
    biased, _ = _fit(old_rule)
    assert abs(biased["price_coef"]) < abs(honest["price_coef"])
    rel = abs(biased["price_coef"] - PLANTED_B) / abs(PLANTED_B)
    assert rel > 0.15, (
        f"the old rule recovered {biased['price_coef']:.2f} against planted "
        f"{PLANTED_B}: this world does not punish it, so the test proves nothing"
    )


# ---------------------------------------------------------------------------
# one decision, one observation
# ---------------------------------------------------------------------------


def _with_revisions(q: pl.DataFrame, n_rev: int = 2) -> pl.DataFrame:
    """Give every quote earlier revisions at higher prices, as a negotiation
    that walked the price down would leave behind."""
    q = q.with_columns(
        pl.format("Q{}", pl.int_range(pl.len()).cast(pl.String)).alias("quote_id"),
        pl.lit(n_rev + 1, dtype=pl.Int64).alias("quote_version"),
    )
    earlier = []
    for v in range(1, n_rev + 1):
        bump = 1.0 + 0.05 * (n_rev + 1 - v)
        earlier.append(
            q.with_columns(
                pl.lit(v, dtype=pl.Int64).alias("quote_version"),
                (pl.col("quote_index") * bump).alias("quote_index"),
                (pl.col("quoted_price_cents") * bump).cast(pl.Int64).alias("quoted_price_cents"),
            )
        )
    return pl.concat([*earlier, q], how="vertical_relaxed")


def test_only_the_latest_revision_of_an_opportunity_is_modeled() -> None:
    q = _with_revisions(_decided_quotes(n=600), n_rev=2)
    kept, dropped = collapse_revisions(q, "opportunity")
    assert dropped == 1200
    assert kept.height == 600
    assert set(kept["quote_version"].to_list()) == {3}


def test_the_unit_is_the_opportunity_when_the_mapping_provides_one() -> None:
    q = _with_revisions(_decided_quotes(n=600))
    _, intake = prepare_quotes(q, _segments(q), _rules())
    assert intake.unit == "opportunity"
    assert intake.excluded["superseded_revision"] == 1200

    _, line_intake = prepare_quotes(q.drop("quote_id"), _segments(q), _rules(quote_unit="auto"))
    assert line_intake.unit == "line"
    assert any("no quote id" in n for n in line_intake.notes)


def test_asking_for_opportunities_without_a_quote_id_fails_actionably() -> None:
    q = _decided_quotes(n=100)
    with pytest.raises(ValueError, match="declares no quote_id"):
        prepare_quotes(q, _segments(q), _rules(quote_unit="opportunity"))


def test_counting_revisions_separately_moves_the_estimate() -> None:
    """Revisions are not extra evidence; treating them as such reweights the
    negotiation toward the prices that had to be revised."""
    q = _with_revisions(_decided_quotes(), n_rev=2)
    collapsed, _ = _fit(q)
    flat, _ = _fit(q.drop("quote_id"), _rules(quote_unit="line"))
    rel_collapsed = abs(collapsed["price_coef"] - PLANTED_B) / abs(PLANTED_B)
    rel_flat = abs(flat["price_coef"] - PLANTED_B) / abs(PLANTED_B)
    assert rel_collapsed <= 0.15
    assert rel_flat > rel_collapsed


# ---------------------------------------------------------------------------
# quotes are not independent draws
# ---------------------------------------------------------------------------


def test_the_cluster_level_is_the_finest_the_data_supports() -> None:
    q = _with_revisions(_decided_quotes(n=800), n_rev=1)
    curve, _ = _fit(q)
    assert curve["cluster_level"] == "quote"
    no_id, _ = _fit(q.drop("quote_id"), _rules(quote_unit="line"))
    assert no_id["cluster_level"] == "customer"
    assert no_id["n_clusters"] == 300
    off, _ = _fit(q, _rules(win_curve_cluster="none"))
    assert off["cluster_level"] == "none" and off["n_clusters"] is None


def test_clustering_widens_the_interval_when_quotes_repeat_within_a_customer() -> None:
    """Twelve quotes each to twenty-five customers is not three hundred
    independent draws, and the standard error has to say so."""
    q = _decided_quotes(n=3000, n_customers=25)
    clustered, _ = _fit(q)
    naive, _ = _fit(q, _rules(win_curve_cluster="none"))
    assert clustered["n_clusters"] == 25
    assert clustered["price_coef_se"] > naive["price_coef_se"]
    assert clustered["price_coef"] == pytest.approx(naive["price_coef"], rel=1e-9)


def test_controls_are_named_on_the_output() -> None:
    curve, _ = _fit(_decided_quotes())
    assert "time" in curve["controls"]
    assert "log_quantity" in curve["controls"]


# ---------------------------------------------------------------------------
# calibration on quotes the curve has not seen
# ---------------------------------------------------------------------------


def test_a_well_specified_curve_is_calibrated_out_of_sample() -> None:
    curve, _ = _fit(_decided_quotes())
    assert curve["holdout_n"] > 0
    assert curve["calibration_status"] == CALIBRATION_OK
    assert curve["calibration_slope"] == pytest.approx(1.0, abs=0.5)
    assert 0 < curve["brier"] < 0.25
    assert curve["auc"] > 0.7
    assert curve["status"] == STATUS_ESTIMATED


def test_a_curve_that_stops_describing_the_market_is_downgraded() -> None:
    """The second year's customers barely care about price. A curve fitted on
    the first year is precise and wrong, and only the held-out check sees it.
    """
    rng = np.random.default_rng(3)
    n = 3000
    index = rng.uniform(0.75, 1.15, n)
    days = np.sort(rng.integers(0, 720, n))
    late = days > 500
    b = np.where(late, -0.5, PLANTED_B)
    a = np.where(late, 0.5 * 0.95, PLANTED_A)
    p = 1.0 / (1.0 + np.exp(-(a + b * index)))
    won = rng.random(n) < p
    start = dt.date(2024, 1, 1)
    q = pl.DataFrame(
        {
            "quote_date": [start + dt.timedelta(days=int(d)) for d in days],
            "customer_id": [f"C{i % 300:03d}" for i in range(n)],
            "item_id": [f"I{i % 40:03d}" for i in range(n)],
            "quantity": rng.integers(1, 40, n).astype(float),
            "quote_index": index,
            "quoted_price_cents": (index * 10_000).astype(np.int64),
            "close_price_cents": pl.Series([None] * n, dtype=pl.Int64),
            "outcome": np.where(won, "W", "L"),
        }
    )
    curve, _ = _fit(q)
    assert curve["calibration_status"] == CALIBRATION_OFF
    assert curve["status"] == STATUS_LOW_PRECISION
    assert "calibration slope" in curve["reasons"]


def test_too_few_held_out_quotes_says_so_and_does_not_block_the_status() -> None:
    """A check that could not be run is reported as not run. It is not a
    failure, and it is not a pass either - the word on the output is
    "not testable".
    """
    curve, _ = _fit(_decided_quotes(), _rules(win_curve_min_holdout=10_000))
    assert curve["calibration_status"] == CALIBRATION_UNTESTED
    assert curve["calibration_slope"] is None
    assert curve["holdout_n"] == 0
    assert curve["status"] == STATUS_ESTIMATED, curve["reasons"]


def test_won_and_lost_counts_are_reported_beside_the_total() -> None:
    curve, _ = _fit(_with_undecided(_decided_quotes()))
    assert curve["n_won"] + curve["n_lost"] == curve["n_quotes"]
    assert curve["n_won"] > 0 and curve["n_lost"] > 0
