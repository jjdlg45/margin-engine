"""Phase 70 (Run 6): the competitor-price path in the causal gate.

Findings 2 and 13. The engine's estimand is the unilateral own-price response
holding observed competitor-price conditions constant, so an industry-wide
cost instrument needs those conditions held constant before the top status is
awarded. These tests pin the adequacy rules (which are structural and
relevance-based, NOT a threshold on collinearity), the gate, the three
estimands, and the downstream consequence.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from margin_engine.config import InstrumentConfig, InstrumentPath, Rules
from margin_engine.sensitivity.market_control import (
    CONTROL_ABSENT,
    CONTROL_ABSORBED,
    CONTROL_ADEQUATE,
    CONTROL_INCOMPLETE,
    CONTROL_NOT_PUBLIC,
    CONTROL_RESTATES_INSTRUMENT,
    CONTROL_STALE,
    CONTROL_UNINFORMATIVE,
    ESTIMAND_DESCRIPTIVE,
    ESTIMAND_MARKET_WIDE,
    ESTIMAND_UNILATERAL,
    PATH_CONTROLLED,
    PATH_GRAIN_TOO_COARSE,
    PATH_NO_PATH,
    PATH_NOT_CONTROLLED,
    SOURCE_ABSENT,
    SOURCE_MAPPED,
    SOURCE_NOT_PUBLIC,
    assess_market_control,
    estimand_for,
    resolve_competitor_path,
    select_market_index,
)

RULES = Rules()
RNG = np.random.default_rng(7)
N = 600


def _cells(n: int = N, per: int = 12) -> np.ndarray:
    return np.repeat(np.arange(n // per), per)[:n]


def _informative_control(n: int = N) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A control that varies within each time cell AND moves prices - what a
    real category-level published index looks like."""
    cells = _cells(n)
    mkt = RNG.normal(0, 0.05, n) + 0.02 * (cells % 5)
    z = np.column_stack([RNG.normal(0, 0.05, n), RNG.normal(0, 0.04, n)])
    price = 0.6 * mkt + 0.5 * z[:, 0] + RNG.normal(0, 0.02, n)
    return mkt, price, z


# ---------------------------------------------------------------------------
# Control selection (the public-source rule, now shared with production)
# ---------------------------------------------------------------------------


def test_absent_index_is_absent() -> None:
    frame, source, notes = select_market_index(None, public_source=None, rules=RULES)
    assert frame is None
    assert source == SOURCE_ABSENT
    assert not notes


def test_non_public_index_is_refused_with_a_note() -> None:
    idx = pl.DataFrame({"item_category": ["CAT01"], "period": ["2024-01"], "market_index": [1.0]})
    frame, source, notes = select_market_index(idx, public_source=False, rules=RULES)
    assert frame is None
    assert source == SOURCE_NOT_PUBLIC
    assert any("public source" in n for n in notes)


def test_public_index_is_accepted() -> None:
    idx = pl.DataFrame({"item_category": ["CAT01"], "period": ["2024-01"], "market_index": [1.0]})
    frame, source, _ = select_market_index(idx, public_source=True, rules=RULES)
    assert frame is not None
    assert source == SOURCE_MAPPED


# ---------------------------------------------------------------------------
# Adequacy: structural and relevance-based
# ---------------------------------------------------------------------------


def _assess(**kw: object) -> object:
    mkt, price, z = _informative_control()
    args: dict[str, object] = {
        "source": SOURCE_MAPPED,
        "log_mkt": mkt,
        "log_price_within": price,
        "instruments_within": z,
        "time_cell": _cells(),
        "coverage": 1.0,
        "lag_periods": 0,
        "rules": RULES,
    }
    args.update(kw)
    return assess_market_control(**args)  # type: ignore[arg-type]


def test_informative_category_index_is_adequate() -> None:
    a = _assess()
    assert a.status == CONTROL_ADEQUATE  # type: ignore[attr-defined]
    assert a.adequate and a.usable  # type: ignore[attr-defined]
    assert a.price_relevance_r2 > 0.01  # type: ignore[attr-defined]


def test_correlation_with_an_instrument_is_not_disqualifying() -> None:
    """THE central rule: a faithful control must correlate with industry-wide
    cost instruments, because that correlation IS the confound it absorbs.
    Only near-exact restatement is refused."""
    cells = _cells()
    z0 = RNG.normal(0, 0.05, N)
    # 85% of the control is the instrument - heavy overlap, still legitimate.
    mkt = 0.85 * z0 + RNG.normal(0, 0.02, N) + 0.01 * (cells % 5)
    price = 0.6 * mkt + RNG.normal(0, 0.02, N)
    a = _assess(
        log_mkt=mkt,
        log_price_within=price,
        instruments_within=np.column_stack([z0, RNG.normal(0, 0.04, N)]),
    )
    assert a.status == CONTROL_ADEQUATE  # type: ignore[attr-defined]
    assert a.instrument_r2 > 0.5  # type: ignore[attr-defined]


def test_near_exact_restatement_is_refused() -> None:
    cells = _cells()
    z0 = RNG.normal(0, 0.05, N) + 0.01 * (cells % 5)
    mkt = z0 + RNG.normal(0, 1e-6, N)  # essentially the instrument itself
    price = 0.6 * mkt + RNG.normal(0, 0.02, N)
    a = _assess(log_mkt=mkt, log_price_within=price, instruments_within=z0.reshape(-1, 1))
    assert a.status == CONTROL_RESTATES_INSTRUMENT  # type: ignore[attr-defined]
    assert not a.usable  # type: ignore[attr-defined]


def test_pure_noise_control_is_refused_as_uninformative() -> None:
    """The failure mode an R-squared rule would have rewarded: a column of
    noise passes every 'not collinear' test while controlling nothing."""
    cells = _cells()
    mkt = RNG.normal(0, 0.05, N) + 0.001 * (cells % 5)
    price = RNG.normal(0, 0.05, N)  # unrelated to the control
    a = _assess(log_mkt=mkt, log_price_within=price)
    assert a.status == CONTROL_UNINFORMATIVE  # type: ignore[attr-defined]
    assert not a.adequate  # type: ignore[attr-defined]


def test_national_monthly_index_is_absorbed_structurally() -> None:
    """One value per period is removed EXACTLY by time fixed effects, so it
    cannot hold anything constant - detected structurally, not by an epsilon."""
    cells = _cells()
    mkt = 0.03 * cells.astype(float)  # constant within each cell
    price = 0.6 * mkt + RNG.normal(0, 0.02, N)
    a = _assess(log_mkt=mkt, log_price_within=price, time_cell=cells)
    assert a.status == CONTROL_ABSORBED  # type: ignore[attr-defined]
    assert a.within_cell_values == pytest.approx(1.0)  # type: ignore[attr-defined]


def test_stale_index_is_refused_before_coverage() -> None:
    a = _assess(lag_periods=4)
    assert a.status == CONTROL_STALE  # type: ignore[attr-defined]


def test_incomplete_index_is_refused() -> None:
    a = _assess(coverage=0.6)
    assert a.status == CONTROL_INCOMPLETE  # type: ignore[attr-defined]


def test_absent_and_non_public_sources_report_distinctly() -> None:
    assert _assess(source=SOURCE_ABSENT, log_mkt=None).status == CONTROL_ABSENT  # type: ignore[attr-defined]
    assert _assess(source=SOURCE_NOT_PUBLIC).status == CONTROL_NOT_PUBLIC  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _inst(
    name: str,
    *,
    varies_at: str = "item_category:month",
    resolved_by: str = "market_price_control",
    channel_grain: str | None = None,
    treatment: str = "handled",
) -> InstrumentConfig:
    return InstrumentConfig(
        name=name,
        column=name,
        varies_at=varies_at,
        exclusion_rationale="documented",
        paths_to_quantity=(
            InstrumentPath(
                channel="competitor_prices",
                treatment=treatment,
                resolved_by=resolved_by,  # type: ignore[arg-type]
                channel_grain=channel_grain,
            ),
        ),
    )


def test_adequate_control_resolves_the_path() -> None:
    r = resolve_competitor_path(
        (_inst("z_tariff"),), control_adequate=True, control_grain="item_category:month"
    )
    assert r.code == PATH_CONTROLLED
    assert r.resolved


def test_inadequate_control_leaves_the_path_open() -> None:
    r = resolve_competitor_path(
        (_inst("z_tariff"),), control_adequate=False, control_grain="item_category:month"
    )
    assert r.code == PATH_NOT_CONTROLLED
    assert not r.resolved


def test_undocumented_path_is_unresolved() -> None:
    r = resolve_competitor_path(
        (_inst("z", resolved_by="unresolved"),),
        control_adequate=True,
        control_grain="item_category:month",
    )
    assert not r.resolved
    assert r.per_instrument["z"] == "unresolved"


def test_no_path_needs_a_written_argument() -> None:
    with pytest.raises(ValueError, match="written argument"):
        InstrumentPath(channel="competitor_prices", treatment="  ", resolved_by="no_path")
    r = resolve_competitor_path(
        (_inst("z", resolved_by="no_path", treatment="this channel cannot exist because..."),),
        control_adequate=False,
        control_grain=None,
    )
    assert r.code == PATH_NO_PATH
    assert r.resolved


def test_control_coarser_than_the_channel_is_refused() -> None:
    """A category index cannot absorb a channel that operates per supplier."""
    r = resolve_competitor_path(
        (_inst("z_cost", varies_at="supplier_id:month"),),
        control_adequate=True,
        control_grain="item_category:month",
    )
    assert r.code == PATH_GRAIN_TOO_COARSE
    assert not r.resolved


def test_declared_channel_grain_is_what_matters() -> None:
    """A supplier-level INSTRUMENT whose competitor-price CHANNEL is declared
    (and argued) to operate at category level is covered by a category index."""
    r = resolve_competitor_path(
        (
            _inst(
                "z_cost",
                varies_at="supplier_id:month",
                channel_grain="item_category:month",
                treatment="customers compare sellers of the same item class",
            ),
        ),
        control_adequate=True,
        control_grain="item_category:month",
    )
    assert r.code == PATH_CONTROLLED


def test_one_unresolved_instrument_contaminates_the_estimate() -> None:
    r = resolve_competitor_path(
        (
            _inst("ok", resolved_by="no_path", treatment="argued"),
            _inst("bad", resolved_by="unresolved"),
        ),
        control_adequate=False,
        control_grain=None,
    )
    assert not r.resolved
    assert r.per_instrument == {"ok": "no_path", "bad": "unresolved"}


def test_no_instruments_cannot_resolve_anything() -> None:
    r = resolve_competitor_path((), control_adequate=True, control_grain="item_category:month")
    assert not r.resolved


# ---------------------------------------------------------------------------
# Estimands
# ---------------------------------------------------------------------------


def test_three_estimands() -> None:
    assert estimand_for(iv_ran=True, path_resolved=True) == (ESTIMAND_UNILATERAL, None)
    name, bias = estimand_for(iv_ran=True, path_resolved=False)
    assert name == ESTIMAND_MARKET_WIDE
    assert bias == "understates_sensitivity"
    # A non-IV estimate is NOT a market-wide cost-shock response: calling it
    # that would assert a cost-shock reading no instrument supported.
    assert estimand_for(iv_ran=False, path_resolved=False) == (ESTIMAND_DESCRIPTIVE, None)
    assert estimand_for(iv_ran=False, path_resolved=True) == (ESTIMAND_DESCRIPTIVE, None)


# ---------------------------------------------------------------------------
# Config shape and the absence of a fail-open switch
# ---------------------------------------------------------------------------


def test_no_switch_disables_the_competitor_gate() -> None:
    """The only routes to the top status are an adequate control or a
    documented absent path - never a configuration flag."""
    keys = set(Rules().sensitivity.model_dump())
    assert not any("require_competitor" in k or "skip_competitor" in k for k in keys)


def test_shipped_instruments_declare_resolution_and_grain() -> None:
    from margin_engine.config import load_rules

    rules = load_rules("config/rules.yaml")
    for inst in rules.sensitivity.instruments:
        path = next(p for p in inst.paths_to_quantity if p.channel == "competitor_prices")
        assert path.resolved_by == "market_price_control"
        assert path.channel_grain == "item_category:month"
        assert path.treatment.strip()
