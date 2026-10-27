"""Phase 72 (Run 6): the total-quantity estimand and its ceiling.

The engine estimates two different things and must never let one pass for the
other. The conditional estimate answers "among customers who bought, how does
quantity respond to price?"; the total-quantity estimate answers "how does
expected quantity respond, counting the months they bought nothing?". The second
is what an opportunity forecast needs and the first cannot supply - but on
planted worlds it is about six times less accurate, so it carries no causal tier
and the engine never prices from it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.panel.build import build_panel
from margin_engine.sensitivity.estimate import (
    STATUS_CAUSAL,
    STATUS_TOTAL_DESCRIPTIVE,
    STATUS_TOTAL_IV,
    _iv_ppml_bootstrap,
    _iv_ppml_sample_losses,
    _iv_ppml_split_panel,
    run_sensitivity,
)

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def _rules():
    return load_rules(REPO / "config" / "rules.yaml")


@pytest.fixture(scope="module")
def extensive_panel(tmp_path_factory: pytest.TempPathFactory):
    """A world whose purchase incidence responds to price, so total and
    conditional elasticities are genuinely different numbers.

    The PANEL is the fixture, not the estimates, so the tests below can
    re-estimate it under different rules without generating another world.
    One world at this scale costs about a gigabyte and most of the runtime of
    this file; several would cost several."""
    out = tmp_path_factory.mktemp("ext")
    cfg = GeneratorConfig.for_preset(Preset.EXTENSIVE_MARGIN, seed=42, n_customers=150, n_items=250)
    generate(cfg, out)
    rules = _rules()
    res = run_pipeline(out / "erp", MAPPING, rules)
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    pt = pl.read_parquet(out / "ground_truth" / "panel_truth.parquet")
    obs = flagged.join(
        pt.select("customer_id", "item_id", "segment_id").unique(),
        on=["customer_id", "item_id"],
        how="left",
    ).with_columns(pl.col("segment_id").alias("segment_leaf"))
    panel = build_panel(
        obs,
        res.reference["items"],
        res.reference["supplier_cost_index"],
        res.reference["tariff_schedule"],
        rules,
        quotes=res.reference.get("quotes"),
        price_list=res.reference.get("price_list"),
    )
    truth: dict[int, dict[str, Any]] = {
        s["segment_id"]: s
        for s in json.loads((out / "ground_truth" / "truth_summary.json").read_text())["segments"]
    }
    return panel, truth


@pytest.fixture(scope="module")
def extensive_world(extensive_panel):
    """The same world estimated under the SHIPPED rules."""
    panel, truth = extensive_panel
    return run_sensitivity(panel, _rules()), truth


def _with(**sensitivity_overrides: Any) -> Rules:
    """The shipped rules with the named sensitivity settings changed."""
    rules = _rules()
    return rules.model_copy(
        update={"sensitivity": rules.sensitivity.model_copy(update=sensitivity_overrides)}
    )


def test_the_generator_plants_two_different_elasticities(extensive_world) -> None:
    _sens, truth = extensive_world
    rows = [s for s in truth.values() if "total_elasticity" in s]
    assert rows, "the preset recorded no planted total elasticity"
    for s in rows:
        # The extensive term is beta_ext x (1 - incidence), so it is largest
        # where incidence has the most room to fall.
        assert s["total_elasticity"] < s["elasticity"], s
        expected = -1.5 * (1.0 - s["mean_incidence_eq_weighted"])
        assert s["extensive_component"] == pytest.approx(expected, abs=1e-3), s
        assert s["total_elasticity"] == pytest.approx(
            s["intensive_component"] + s["extensive_component"], abs=1e-6
        )


def test_total_quantity_tracks_the_extensive_margin(extensive_world) -> None:
    """The point of the estimand: it must be closer to the planted TOTAL
    elasticity than to the conditional one. Otherwise it is not measuring the
    extensive margin, it is just sitting further from zero."""
    sens, truth = extensive_world
    to_total, to_conditional = [], []
    for r in sens.to_dicts():
        s = truth.get(r["segment"])
        if s is None or "total_elasticity" not in s or r.get("iv_ppml") is None:
            continue
        if r.get("imputed_price_share") is not None and r["imputed_price_share"] > 0.5:
            continue  # thin segments are demoted; see the gate test below
        to_total.append(abs(r["iv_ppml"] - s["total_elasticity"]))
        to_conditional.append(abs(r["iv_ppml"] - s["elasticity"]))
    assert to_total, "no segment produced a total-quantity estimate"
    med_total = sorted(to_total)[len(to_total) // 2]
    med_cond = sorted(to_conditional)[len(to_conditional) // 2]
    print(f"\ntotal-quantity error vs total {med_total:.3f}, vs conditional {med_cond:.3f}")
    assert med_total < med_cond, (med_total, med_cond)


def test_the_total_quantity_ladder_has_no_causal_tier(extensive_world) -> None:
    sens, _truth = extensive_world
    statuses = set(sens["iv_ppml_status"].drop_nulls().to_list())
    assert statuses, "no total-quantity statuses were produced"
    assert STATUS_CAUSAL not in statuses
    assert not any("causal" in s for s in statuses), statuses
    # And every estimate carries the reason it can go no higher.
    for r in sens.to_dicts():
        if r.get("iv_ppml") is None:
            continue
        assert "no causal tier" in (r.get("iv_ppml_reasons") or ""), r["segment"]


def test_an_imputation_heavy_segment_is_demoted(extensive_world) -> None:
    """The zero rows carry the whole extensive-margin signal, so a segment whose
    zero rows are mostly imputed cannot support the estimand."""
    sens, _truth = extensive_world
    heavy = [
        r
        for r in sens.to_dicts()
        if (r.get("imputed_price_share") or 0.0) > 0.5 and r.get("iv_ppml") is not None
    ]
    light = [
        r
        for r in sens.to_dicts()
        if (r.get("imputed_price_share") or 0.0) <= 0.5 and r.get("iv_ppml") is not None
    ]
    # The contrast matters: demotion must be driven by the imputed share, not
    # applied to everything.
    assert any(r["iv_ppml_status"] == STATUS_TOTAL_IV for r in light), (
        "no segment reached the instrumented tier, so the demotion test proves nothing"
    )
    if not heavy:
        pytest.skip("this world produced no imputation-heavy segment")
    for r in heavy:
        assert r["iv_ppml_status"] == STATUS_TOTAL_DESCRIPTIVE, r["segment"]
        assert "imputed price" in r["iv_ppml_reasons"]


def test_the_two_estimands_use_distinct_status_codes(extensive_world) -> None:
    """Reusing one string across two ladders would make it ambiguous in the
    exports, the bundle, drift comparisons and the tracker."""
    sens, _truth = extensive_world
    conditional = set(sens["status"].drop_nulls().to_list())
    total = set(sens["iv_ppml_status"].drop_nulls().to_list())
    assert not (conditional & total), conditional & total
    assert all(s.endswith("_total_quantity") for s in total), total


def test_total_quantity_estimates_never_reach_the_optimizer(extensive_world) -> None:
    """Nothing prices from the total-quantity number: it is not routed into the
    elasticity the optimizer and opportunity sizing read."""
    sens, _truth = extensive_world
    cols = set(sens.columns)
    assert "iv_ppml" in cols
    # The optimizer reads `iv`; the total estimate lives in its own column and
    # the two are never equal where both exist (they are different estimands).
    both = sens.filter(pl.col("iv").is_not_null() & pl.col("iv_ppml").is_not_null())
    if both.height:
        assert not (both["iv"] == both["iv_ppml"]).all()


# ---------------------------------------------------------------------------
# Sample-loss accounting: rows AND units, by reason, adding to the total
# ---------------------------------------------------------------------------


def _loss_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "qty": pl.Float64,
            "log_price": pl.Float64,
            "pair": pl.Int64,
            "month_idx": pl.Int64,
            "z_cost": pl.Float64,
            "contract_id": pl.String,
        },
    )


def _lrow(pair: int, month: int, qty: float, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "qty": qty,
        "log_price": 2.0,
        "pair": pair,
        "month_idx": month,
        "z_cost": 0.1,
        "contract_id": None,
    }
    base.update(over)
    return base


def test_sample_losses_count_every_reason_in_rows_and_units() -> None:
    """Each reason is measured on what survived the previous one, so the parts
    add to the whole instead of charging one row to two reasons."""
    # Four healthy pairs over four months keep every period populated, so the
    # singleton sweep has nothing to take and each reason below is isolated.
    rows = [_lrow(p, m, 5.0) for p in range(1, 5) for m in range(4)]
    rows += [_lrow(10, 0, 11.0, contract_id="K1"), _lrow(10, 1, 13.0, contract_id="K1")]
    rows += [_lrow(11, 0, 17.0, log_price=None)] + [_lrow(11, m, 2.0) for m in range(1, 4)]
    rows += [_lrow(12, 0, 23.0, z_cost=None)] + [_lrow(12, m, 2.0) for m in range(1, 4)]
    text, dropped_rows, dropped_units = _iv_ppml_sample_losses(
        _loss_frame(rows), ["z_cost"], "month_idx"
    )
    assert dropped_rows == 4, text
    assert dropped_units == 11 + 13 + 17 + 23, text
    assert "priced on a contract" in text
    assert "no price" in text
    assert "no z cost instrument value" in text
    # Nothing was absorbed or separated here, so neither is claimed.
    assert "single observation" not in text
    assert "separation" not in text
    # The headline is measured against the whole frame, not a running subtotal.
    assert f"{dropped_rows} of {len(rows)} rows" in text


def test_a_pair_that_never_buys_is_reported_as_separated() -> None:
    """The customers who stopped buying are the case this estimand exists for,
    so the estimator dropping them has to be visible rather than silent."""
    rows = [_lrow(p, m, 5.0) for p in (1, 2, 3) for m in range(4)]
    rows += [_lrow(9, m, 0.0) for m in range(4)]
    text, dropped_rows, dropped_units = _iv_ppml_sample_losses(
        _loss_frame(rows), ["z_cost"], "month_idx"
    )
    assert dropped_rows == 4, text
    assert dropped_units == 0  # a pair that never buys costs no volume
    assert "never buys" in text and "separation" in text


def test_separation_cascades_through_both_fixed_effects() -> None:
    """Dropping a pair that never buys can leave a period in which nothing was
    bought, which separates in turn, so the sweep repeats until it is stable."""
    # Months 0-2 have purchases; in month 3 nobody buys except the pair that
    # never buys at all - so month 3 only becomes empty once that pair goes.
    rows = [_lrow(p, m, 5.0) for p in (1, 2, 3) for m in range(3)]
    rows += [_lrow(p, 3, 0.0) for p in (1, 2, 3)]
    rows += [_lrow(9, m, 0.0) for m in range(4)]
    text, dropped_rows, _units = _iv_ppml_sample_losses(_loss_frame(rows), ["z_cost"], "month_idx")
    # pair 9's four rows, then month 3's three remaining rows once it is empty
    assert dropped_rows == 7, text


def test_nothing_lost_says_so_plainly() -> None:
    rows = [_lrow(p, m, 5.0) for p in (1, 2) for m in range(3)]
    text, dropped_rows, dropped_units = _iv_ppml_sample_losses(
        _loss_frame(rows), ["z_cost"], "month_idx"
    )
    assert (dropped_rows, dropped_units) == (0, 0)
    assert text == "every one of the 6 segment rows reached the fit"


def test_the_loss_accounting_survives_a_fit_that_fails() -> None:
    """The reason a fit failed is usually in the accounting, so it is recorded
    even when there is no estimate to attach it to."""
    rows = [_lrow(p, m, 0.0) for p in (1, 2, 3) for m in range(4)]
    text, dropped_rows, _u = _iv_ppml_sample_losses(_loss_frame(rows), ["z_cost"], "month_idx")
    assert dropped_rows == 12
    assert "never buys" in text


# ---------------------------------------------------------------------------
# Inference: two standard errors, each labeled with what it leaves out
# ---------------------------------------------------------------------------


def test_the_analytic_standard_error_ships_and_says_what_it_omits(extensive_world) -> None:
    sens, _truth = extensive_world
    fitted = [r for r in sens.to_dicts() if r.get("iv_ppml") is not None]
    assert fitted, "no total-quantity estimate was produced"
    for r in fitted:
        assert r["iv_ppml_se_analytic"] is not None and r["iv_ppml_se_analytic"] > 0
        assert "omits first-stage sampling noise" in r["iv_ppml_inference"]


def test_without_replicates_the_absence_of_a_resampled_range_is_stated(extensive_world) -> None:
    """The shipped configuration computes no resampled interval. That has to
    read as a stated absence, not as a field that quietly went missing."""
    sens, _truth = extensive_world
    checked = 0
    for r in sens.to_dicts():
        if r.get("iv_ppml") is None:
            continue
        checked += 1
        assert r["iv_ppml_range_low"] is None and r["iv_ppml_range_high"] is None
        assert r["iv_ppml_bootstrap_reps"] == 0
        assert "No resampled interval was computed in this run" in r["iv_ppml_inference"]
    assert checked


def test_the_bootstrap_carries_first_stage_noise_and_brackets_the_point(
    extensive_panel,
) -> None:
    """Enabled, the pair cluster bootstrap produces a usable range around the
    point estimate, refitting the first stage in every replicate."""
    panel, _truth = extensive_panel
    sens = run_sensitivity(
        panel, _with(iv_ppml_bootstrap_reps=12, iv_ppml_zero_row_robustness=False)
    )
    fitted = [r for r in sens.to_dicts() if r.get("iv_ppml_se_bootstrap") is not None]
    assert fitted, "the bootstrap produced no standard error on any segment"
    for r in fitted:
        assert r["iv_ppml_se_bootstrap"] > 0
        assert r["iv_ppml_range_low"] < r["iv_ppml_range_high"]
        assert r["iv_ppml_bootstrap_reps"] > 0
        assert "first stage refitted in each" in r["iv_ppml_inference"]


def test_the_bootstrap_refuses_rather_than_resampling_too_few_pairs() -> None:
    pdf = pd.DataFrame(
        {
            "qty": [1.0, 2.0] * 10,
            "log_price": [2.0, 2.1] * 10,
            "pair": list(range(10)) * 2,
            "month_idx": [0] * 10 + [1] * 10,
            "z_cost": [0.1, 0.2] * 10,
            "cluster_month": [0] * 10 + [1] * 10,
        }
    )
    se, lo, hi, reps, note = _iv_ppml_bootstrap(
        pdf, ["z_cost"], "month_idx", ["cluster_month"], use_control=False, reps=5, seed=1
    )
    assert (se, lo, hi, reps) == (None, None, None, 0)
    assert "too few to resample" in note


# ---------------------------------------------------------------------------
# Split-panel jackknife: computed, reported, not adopted
# ---------------------------------------------------------------------------


def test_the_split_panel_correction_is_reported_not_substituted(extensive_panel) -> None:
    panel, _truth = extensive_panel
    sens = run_sensitivity(
        panel, _with(iv_ppml_split_panel_jackknife=True, iv_ppml_zero_row_robustness=False)
    )
    corrected = [r for r in sens.to_dicts() if r.get("iv_ppml_spj") is not None]
    assert corrected, "the split-panel correction ran on no segment"
    for r in corrected:
        # The published point estimate is the UNCORRECTED one.
        assert r["iv_ppml_spj_bias"] == pytest.approx(r["iv_ppml"] - r["iv_ppml_spj"], abs=1e-9)
        assert "not adopted" in r["iv_ppml_spj_detail"]


def test_the_split_panel_correction_refuses_a_panel_it_cannot_halve() -> None:
    pdf = pd.DataFrame(
        {
            "qty": [1.0] * 8,
            "log_price": [2.0] * 8,
            "pair": [0, 1] * 4,
            "month_idx": [0, 0, 1, 1, 2, 2, 3, 3],
            "z_cost": [0.1] * 8,
            "cluster_month": [0, 0, 1, 1, 2, 2, 3, 3],
        }
    )
    spj, bias, detail = _iv_ppml_split_panel(
        pdf, ["z_cost"], "month_idx", ["cluster_month"], use_control=False, beta_full=-1.0
    )
    assert (spj, bias) == (None, None)
    assert "too few to halve" in detail


# ---------------------------------------------------------------------------
# Alternative zero-row assumptions
# ---------------------------------------------------------------------------


def test_a_sensitive_estimate_is_demoted_when_the_zero_row_guess_decides_it(
    extensive_panel,
) -> None:
    """With the tolerance set to nothing, any movement at all under an
    alternative zero-row assumption must demote the estimand and say why. This
    pins the mechanism, not the size of the gap on any particular world."""
    panel, _truth = extensive_panel
    sens = run_sensitivity(panel, _with(iv_ppml_max_zero_row_gap=1e-9))
    tested = [r for r in sens.to_dicts() if r.get("iv_ppml_zero_row_gap") is not None]
    assert tested, "the zero-row assumptions were varied on no segment"
    for r in tested:
        assert r["iv_ppml_status"] == STATUS_TOTAL_DESCRIPTIVE
        assert "zero-row price and availability assumptions are varied" in r["iv_ppml_reasons"]
        assert "against a" in r["iv_ppml_zero_row_detail"]


def test_the_zero_row_check_varies_both_assumptions(extensive_world) -> None:
    sens, _truth = extensive_world
    detail = " ".join(r.get("iv_ppml_zero_row_detail") or "" for r in sens.to_dicts())
    assert "priced by imputation" in detail
    assert "availability rests on a single buyer" in detail


# ---------------------------------------------------------------------------
# Zero-row provenance flags
# ---------------------------------------------------------------------------


def test_every_zero_row_declares_what_it_rests_on(extensive_panel) -> None:
    panel, _truth = extensive_panel
    zeros = panel.filter(pl.col("qty") == 0)
    bought = panel.filter(pl.col("qty") > 0)
    assert zeros.height and bought.height
    # A purchase observes all three; a no-purchase month observes none of them
    # and never claims to.
    assert bought["assortment_basis"].unique().to_list() == ["observed"]
    assert bought["availability_basis"].unique().to_list() == ["observed"]
    assert bought["zero_reason_basis"].unique().to_list() == ["not_applicable"]
    assert zeros["assortment_basis"].unique().to_list() == ["inferred"]
    assert zeros["availability_basis"].unique().to_list() == ["proxied"]
    # No export records WHY a customer did not buy, so the engine never guesses.
    assert zeros["zero_reason_basis"].unique().to_list() == ["unknown"]
    assert (zeros["availability_buyers"] > 0).all()
