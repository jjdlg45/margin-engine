"""Competitor-price path: control selection and adequacy (Run 6, Phase 70).

The estimand this engine reports is the **unilateral own-price response
holding observed competitor-price conditions constant**. Both shipped
instruments are industry-wide cost shocks, so they move competitor prices too:
without something holding those conditions constant, what the IV identifies is
a response to a market-wide cost shock, which understates unilateral price
sensitivity (a customer facing an industry-wide increase has nowhere cheaper
to go). That is a different estimand, and Phase 70 stops the engine calling it
causal.

Two decisions live here, both pure functions so tests and the validation
scripts exercise exactly what production does (before this phase the
public-source rule lived inline in the pipeline and every test bypassed it):

1. ``select_market_index`` — may this mapped table be used as a control at all
   (Phase 37's public-source rule)?
2. ``assess_market_control`` — is the control ADEQUATE for the gate?

Adequacy is structural and relevance-based, never a magnitude threshold on
collinearity. That distinction matters: a faithful market-price control MUST
correlate with industry-wide cost instruments, because that correlation is the
confound it exists to absorb. Rejecting a control for being correlated with
the instruments would reward a column of noise and punish an informative
series — exactly backwards. So the checks are:

- **fresh**: the trailing edge of the series reaches the estimation window
  (evaluated first, on the covered periods, so staleness is reachable and
  disjoint from coverage);
- **complete**: the share of estimation rows the series actually covers;
- **not absorbed** (structural): more than one distinct control value inside
  each time-fixed-effect cell. A national monthly series is absorbed EXACTLY
  by month fixed effects, so it cannot hold anything constant — this is a
  structural fact, tested structurally, not an epsilon on a variance;
- **grain matches the instruments**: a control coarser than an instrument's
  own variation cannot absorb that instrument's competitor-price movement, so
  that instrument's path stays unresolved unless config argues it away;
- **informative about prices** (relevance): the control must explain own-price
  movement after fixed effects with the right sign. A series unrelated to
  prices is not evidence of a competitor-price condition;
- **not a restatement**: only NEAR-EXACT collinearity with an instrument is
  rejected (it would destroy identification rather than control a confound);
  the first stage before and after the control is reported either way and the
  existing F threshold does the rest of the work.

There is deliberately NO config switch that disables this gate. The only two
routes to ``causal_elasticity_supported`` are an adequate control or an
instrument whose competitor-price path is documented as absent WITH a written
justification. A one-word config change must not be able to award a causal
claim the method does not support.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from margin_engine.config import InstrumentConfig, Rules
from margin_engine.ingest.mapping import SourceProvenance

# --- control source (Phase 37 public-source rule) ---------------------------
SOURCE_ABSENT = "market_control_absent"
SOURCE_NOT_PUBLIC = "market_control_not_public"
#: Run 6, Phase 80: the series has no recorded provenance, is not the kind of
#: series this control needs, or comes from a publisher nobody approved.
SOURCE_NO_PROVENANCE = "market_control_no_provenance"
SOURCE_WRONG_KIND = "market_control_wrong_series_kind"
SOURCE_NOT_ALLOWLISTED = "market_control_provider_not_allowed"
SOURCE_MAPPED = "market_control_mapped"

# --- adequacy outcomes -------------------------------------------------------
CONTROL_ADEQUATE = "market_control_adequate"
CONTROL_ABSENT = "market_control_absent"
CONTROL_NOT_PUBLIC = "market_control_not_public"
CONTROL_NO_PROVENANCE = "market_control_no_provenance"
CONTROL_WRONG_KIND = "market_control_wrong_series_kind"
CONTROL_NOT_ALLOWLISTED = "market_control_provider_not_allowed"
CONTROL_STALE = "market_control_stale"
CONTROL_INCOMPLETE = "market_control_incomplete"
CONTROL_ABSORBED = "market_control_absorbed_by_time_effects"
CONTROL_UNINFORMATIVE = "market_control_uninformative"
CONTROL_RESTATES_INSTRUMENT = "market_control_restates_instrument"

# --- competitor-path resolution ----------------------------------------------
PATH_CONTROLLED = "controlled_by_market_index"
PATH_NO_PATH = "no_competitor_price_path"
PATH_NOT_CONTROLLED = "not_controlled"
PATH_GRAIN_TOO_COARSE = "control_coarser_than_instrument"

# --- estimands ----------------------------------------------------------------
ESTIMAND_UNILATERAL = "unilateral_own_price_response"
ESTIMAND_MARKET_WIDE = "market_wide_cost_shock_response"
ESTIMAND_DESCRIPTIVE = "descriptive_association"

BIAS_UNDERSTATES = "understates_sensitivity"


@dataclass(frozen=True)
class ControlAssessment:
    """What the control is, and whether it can hold competitor-price
    conditions constant for this segment."""

    status: str
    usable: bool  # may be partialled into the estimate at all
    adequate: bool  # may support the causal gate
    coverage: float | None = None
    lag_periods: int | None = None
    within_cell_values: float | None = None  # mean distinct values per time cell
    price_relevance_r2: float | None = None
    price_relevance_coef: float | None = None
    instrument_r2: float | None = None
    detail: str = ""

    @property
    def absent(self) -> bool:
        return self.status in (
            CONTROL_ABSENT,
            CONTROL_NOT_PUBLIC,
            CONTROL_NO_PROVENANCE,
            CONTROL_WRONG_KIND,
            CONTROL_NOT_ALLOWLISTED,
        )


def select_market_index(
    market_index: pl.DataFrame | None,
    *,
    provenance: SourceProvenance | None,
    rules: Rules,
) -> tuple[pl.DataFrame | None, str, list[str]]:
    """Whether a market index may serve as the competitor-price control.

    Phase 37 asked the mapping for a boolean: is this public? Run 6
    (Phase 80, Finding 18) asks it to account for the series instead -
    provider, url, retrieval time, licence, cadence, coverage, aggregation
    level and kind - and reads three things from that.

    1. **Is it accounted for at all?** No provenance, no control. A series
       nobody can place is not evidence that competitor prices were held
       constant.
    2. **Is it the right kind of series?** A producer price index is a COST
       series and belongs in the instrument pipeline. The competitor-price
       control needs a market SELLING-price index. A cost index here would
       look like a control and would not be one - the same cost shocks that
       identify the elasticity also move it - so it is refused by name.
    3. **Is the publisher one this deployment accepts?** An allowlist is off
       by default, because most deployments have not drawn one up; when it is
       on, an unlisted provider is refused rather than waved through.

    Returns (frame or None, source code, notes). The notes name the
    provenance, so a reader can see what the control actually is.
    """
    notes: list[str] = []
    if market_index is None:
        return None, SOURCE_ABSENT, notes
    cc = rules.compliance
    if provenance is None:
        if cc.market_index_provenance_required:
            notes.append(
                "market price index NOT used as an elasticity control: the mapping "
                "records no provenance for it (provider, url, retrieved_utc, "
                "license, update_cadence, coverage, aggregation_level, "
                "series_kind). A series nobody can account for is not evidence "
                "that competitor prices were held constant; see "
                "docs/methodology.md, Pricing-law considerations"
            )
            return None, SOURCE_NO_PROVENANCE, notes
        notes.append(
            "market price index used without recorded provenance "
            "(compliance.market_index_provenance_required is off)"
        )
        return market_index, SOURCE_MAPPED, notes
    if provenance.series_kind != "selling_price_index":
        notes.append(
            "market price index NOT used as an elasticity control: its declared "
            f"series_kind is {provenance.series_kind}, and the competitor-price "
            "control needs a market selling-price index. A cost index belongs in "
            "the instrument pipeline, where the same cost movements are the "
            "identifying variation rather than a confound to hold constant"
        )
        return None, SOURCE_WRONG_KIND, notes
    allowed = cc.market_index_allowed_providers
    if cc.market_index_require_allowlist:
        if not allowed:
            notes.append(
                "market price index NOT used as an elasticity control: an "
                "allowlist is required but compliance."
                "market_index_allowed_providers is empty, so no publisher is "
                "approved"
            )
            return None, SOURCE_NOT_ALLOWLISTED, notes
        if provenance.provider not in allowed:
            notes.append(
                "market price index NOT used as an elasticity control: publisher "
                f"{provenance.provider} is not in the approved list "
                f"{list(allowed)}"
            )
            return None, SOURCE_NOT_ALLOWLISTED, notes
    elif allowed and provenance.provider not in allowed:
        notes.append(
            f"market price index publisher {provenance.provider} is not in "
            f"compliance.market_index_allowed_providers {list(allowed)}; the "
            "allowlist is not enforced (market_index_require_allowlist is off), "
            "so the control is used and this is reported"
        )
    notes.append(
        f"market price index control: {provenance.series_kind} published by "
        f"{provenance.provider} ({provenance.url}), retrieved "
        f"{provenance.retrieved_utc}, licence {provenance.license}, updated "
        f"{provenance.update_cadence}, covering {provenance.coverage} at "
        f"{provenance.aggregation_level}"
    )
    return market_index, SOURCE_MAPPED, notes


def _resid_on(v: np.ndarray, m: np.ndarray) -> np.ndarray:
    denom = float(m @ m)
    if denom <= 0:
        return v
    return v - m * (float(m @ v) / denom)


def assess_market_control(
    *,
    source: str,
    log_mkt: np.ndarray | None,
    log_price_within: np.ndarray | None,
    instruments_within: np.ndarray | None,
    time_cell: np.ndarray | None,
    coverage: float | None,
    lag_periods: int | None,
    rules: Rules,
) -> ControlAssessment:
    """Adequacy of the market control for one segment.

    All arrays are on the segment's estimation sample. `log_mkt` is the raw
    control; `log_price_within` and `instruments_within` are already
    within-transformed (pair and time fixed effects removed), which is the
    space the control actually operates in. `time_cell` carries the time
    fixed-effect cell per row (role x month where roles are known), used for
    the structural absorption test.
    """
    sc = rules.sensitivity
    if source in (
        SOURCE_NOT_PUBLIC,
        SOURCE_NO_PROVENANCE,
        SOURCE_WRONG_KIND,
        SOURCE_NOT_ALLOWLISTED,
    ):
        detail = {
            SOURCE_NOT_PUBLIC: (
                "a market price index is mapped but not declared a public source, "
                "so it is refused as a control"
            ),
            SOURCE_NO_PROVENANCE: (
                "a market price index is mapped with no recorded provenance, so it "
                "is refused as a control"
            ),
            SOURCE_WRONG_KIND: (
                "the mapped market index is not a selling-price series, so it "
                "cannot stand in for competitor prices"
            ),
            SOURCE_NOT_ALLOWLISTED: (
                "the mapped market index comes from a publisher this deployment has not approved"
            ),
        }[source]
        return ControlAssessment(
            status={
                SOURCE_NOT_PUBLIC: CONTROL_NOT_PUBLIC,
                SOURCE_NO_PROVENANCE: CONTROL_NO_PROVENANCE,
                SOURCE_WRONG_KIND: CONTROL_WRONG_KIND,
                SOURCE_NOT_ALLOWLISTED: CONTROL_NOT_ALLOWLISTED,
            }[source],
            usable=False,
            adequate=False,
            detail=detail,
        )
    if source == SOURCE_ABSENT or log_mkt is None or log_price_within is None:
        return ControlAssessment(
            status=CONTROL_ABSENT,
            usable=False,
            adequate=False,
            detail="no market price index is mapped for this run",
        )

    # 1. Freshness first, on the covered periods, so this code is reachable
    #    and does not hide behind incomplete coverage.
    if lag_periods is not None and lag_periods > sc.market_control_max_staleness_periods:
        return ControlAssessment(
            status=CONTROL_STALE,
            usable=True,
            adequate=False,
            coverage=coverage,
            lag_periods=lag_periods,
            detail=(
                f"the index stops {lag_periods} period(s) before the estimation "
                f"window ends, beyond the {sc.market_control_max_staleness_periods} "
                "allowed: it cannot describe current competitor-price conditions"
            ),
        )

    # 2. Completeness over the estimation rows.
    if coverage is not None and coverage < sc.market_control_min_coverage:
        return ControlAssessment(
            status=CONTROL_INCOMPLETE,
            usable=True,
            adequate=False,
            coverage=coverage,
            lag_periods=lag_periods,
            detail=(
                f"the index covers {coverage:.1%} of this segment's estimation rows, "
                f"below the required {sc.market_control_min_coverage:.0%}"
            ),
        )

    # 3. Structural absorption: a series constant inside a time fixed-effect
    #    cell is removed exactly by that fixed effect, so it holds nothing
    #    constant. Tested by counting distinct values per cell, not by a
    #    variance epsilon (which would measure demeaning residue).
    within_cell = None
    if time_cell is not None and len(time_cell) == len(log_mkt):
        order = np.argsort(time_cell, kind="stable")
        cells, starts = np.unique(time_cell[order], return_index=True)
        counts = []
        for i, _c in enumerate(cells):
            lo = starts[i]
            hi = starts[i + 1] if i + 1 < len(starts) else len(order)
            counts.append(len(np.unique(np.round(log_mkt[order[lo:hi]], 12))))
        within_cell = float(np.mean(counts)) if counts else 0.0
        if within_cell <= 1.0 + 1e-12:
            return ControlAssessment(
                status=CONTROL_ABSORBED,
                usable=True,
                adequate=False,
                coverage=coverage,
                lag_periods=lag_periods,
                within_cell_values=within_cell,
                detail=(
                    "the index takes one value per time period, so the time fixed "
                    "effects absorb it exactly: it cannot hold competitor-price "
                    "conditions constant (a national series needs category or "
                    "regional detail to do that)"
                ),
            )

    # 4. Relevance: the control must explain own-price movement after fixed
    #    effects. A series unrelated to prices is not evidence of a
    #    competitor-price condition, however clean it looks.
    m = log_mkt - log_mkt.mean()
    denom = float(m @ m)
    coef = r2 = None
    if denom > 0:
        coef = float(m @ log_price_within) / denom
        resid = log_price_within - m * coef
        ss_tot = float(log_price_within @ log_price_within)
        r2 = float(1.0 - (resid @ resid) / ss_tot) if ss_tot > 0 else 0.0
    if coef is None or r2 is None or r2 < sc.market_control_min_price_r2 or coef <= 0:
        return ControlAssessment(
            status=CONTROL_UNINFORMATIVE,
            usable=True,
            adequate=False,
            coverage=coverage,
            lag_periods=lag_periods,
            within_cell_values=within_cell,
            price_relevance_r2=r2,
            price_relevance_coef=coef,
            detail=(
                "the index explains "
                + (f"{r2:.1%}" if r2 is not None else "none")
                + " of within-pair price movement"
                + (" with the wrong sign" if (coef is not None and coef <= 0) else "")
                + f", below the {sc.market_control_min_price_r2:.1%} needed to count as "
                "evidence of a competitor-price condition"
            ),
        )

    # 5. Near-exact restatement of an instrument destroys identification
    #    rather than controlling a confound. Only that extreme is refused;
    #    ordinary correlation with industry-wide cost instruments is EXPECTED.
    inst_r2 = None
    if instruments_within is not None and instruments_within.size and denom > 0:
        worst = 0.0
        for j in range(instruments_within.shape[1]):
            # Both series are demeaned here so the overlap measures shared
            # VARIATION. Production inputs arrive within-transformed already;
            # the function does not rely on that.
            z = instruments_within[:, j] - instruments_within[:, j].mean()
            ss = float(z @ z)
            if ss <= 0:
                continue
            resid_z = _resid_on(z, m)
            worst = max(worst, float(1.0 - (resid_z @ resid_z) / ss))
        inst_r2 = worst
        if inst_r2 >= sc.market_control_restatement_r2:
            return ControlAssessment(
                status=CONTROL_RESTATES_INSTRUMENT,
                usable=False,
                adequate=False,
                coverage=coverage,
                lag_periods=lag_periods,
                within_cell_values=within_cell,
                price_relevance_r2=r2,
                price_relevance_coef=coef,
                instrument_r2=inst_r2,
                detail=(
                    f"the index restates an instrument almost exactly (R-squared "
                    f"{inst_r2:.3f}): controlling for it would remove the variation "
                    "the instrument identifies, not a separate confound"
                ),
            )

    return ControlAssessment(
        status=CONTROL_ADEQUATE,
        usable=True,
        adequate=True,
        coverage=coverage,
        lag_periods=lag_periods,
        within_cell_values=within_cell,
        price_relevance_r2=r2,
        price_relevance_coef=coef,
        instrument_r2=inst_r2,
        detail=(
            "the index is accounted for, is a selling-price series, is current, "
            "covers the estimation window, varies within each time period, and "
            "explains within-pair price movement, so it can hold observed "
            "competitor-price conditions constant"
        ),
    )


@dataclass(frozen=True)
class PathResolution:
    """Whether the competitor-price path is closed for EVERY instrument. The
    estimate uses them jointly, so one unresolved instrument contaminates it."""

    code: str
    resolved: bool
    detail: str
    per_instrument: dict[str, str] = field(default_factory=dict)


def _control_grain_covers(control_grain: str | None, varies_at: str) -> bool:
    """A control can absorb an instrument's competitor-price movement only if
    it is at least as fine as that instrument. `varies_at` is the config's
    declaration (e.g. "supplier_id:month"); `control_grain` is the index's own
    join grain (e.g. "item_category:month")."""
    if control_grain is None:
        return False
    if control_grain == varies_at:
        return True
    coarse_to_fine = {
        "item_category:month": {"item_category:month"},
        "supplier_id:month": {"supplier_id:month", "item_category:month"},
    }
    # A supplier-level instrument is NOT covered by a category-level control:
    # several suppliers share a category, so supplier-specific competitor
    # movement is not in a category average.
    return varies_at in coarse_to_fine.get(control_grain, set())


def resolve_competitor_path(
    instruments: tuple[InstrumentConfig, ...],
    *,
    control_adequate: bool,
    control_grain: str | None = None,
) -> PathResolution:
    """Read each instrument's declared competitor-price path and decide.

    An instrument resolves the path when its config declares the path absent
    (`no_path`, which requires a written treatment) or when it declares the
    path handled by the market control AND that control is adequate at a grain
    at least as fine as the instrument's own variation.
    """
    if not instruments:
        return PathResolution(
            code=PATH_NOT_CONTROLLED,
            resolved=False,
            detail="no instruments are configured, so no exclusion argument exists",
        )
    per: dict[str, str] = {}
    ok_all = True
    all_no_path = True
    grain_problem = False
    for inst in instruments:
        declared = {p.channel: p for p in inst.paths_to_quantity}
        path = declared.get("competitor_prices")
        resolved_by = path.resolved_by if path is not None else "unresolved"
        justified = bool(path is not None and path.treatment.strip())
        if resolved_by == "no_path" and justified:
            per[inst.name] = "no_path"
            ok_all = ok_all and True
            continue
        all_no_path = False
        if resolved_by == "market_price_control":
            # Compare against the CHANNEL's grain where config declares one
            # (with its argument); otherwise against the instrument's own
            # variation, the conservative reading.
            channel_grain = (path.channel_grain if path is not None else None) or inst.varies_at
            fine_enough = _control_grain_covers(control_grain, channel_grain)
            if control_adequate and fine_enough:
                per[inst.name] = "controlled"
                continue
            if control_adequate and not fine_enough:
                per[inst.name] = "control_too_coarse"
                grain_problem = True
                ok_all = False
                continue
            per[inst.name] = "control_inadequate"
            ok_all = False
            continue
        per[inst.name] = "unresolved"
        ok_all = False
    if ok_all and all_no_path:
        return PathResolution(
            code=PATH_NO_PATH,
            resolved=True,
            detail=(
                "every instrument's competitor-price path is documented as absent, "
                "with a written argument in config"
            ),
            per_instrument=per,
        )
    if ok_all:
        return PathResolution(
            code=PATH_CONTROLLED,
            resolved=True,
            detail=(
                "observed competitor-price conditions are held constant by the "
                "public market price index for every instrument that needs it"
            ),
            per_instrument=per,
        )
    if grain_problem:
        return PathResolution(
            code=PATH_GRAIN_TOO_COARSE,
            resolved=False,
            detail=(
                "the market price index is coarser than an instrument's own "
                "variation, so it cannot absorb that instrument's competitor-price "
                "movement: " + ", ".join(f"{k} ({v})" for k, v in sorted(per.items()))
            ),
            per_instrument=per,
        )
    return PathResolution(
        code=PATH_NOT_CONTROLLED,
        resolved=False,
        detail=(
            "at least one instrument's competitor-price path is neither controlled "
            "nor documented as absent: " + ", ".join(f"{k} ({v})" for k, v in sorted(per.items()))
        ),
        per_instrument=per,
    )


def estimand_for(*, iv_ran: bool, path_resolved: bool) -> tuple[str, str | None]:
    """(estimand, bias direction). Three values, not two: a non-IV estimate is
    a descriptive association, NOT a market-wide cost-shock response - calling
    it that would assert a cost-shock interpretation no instrument supported."""
    if not iv_ran:
        return ESTIMAND_DESCRIPTIVE, None
    if path_resolved:
        return ESTIMAND_UNILATERAL, None
    return ESTIMAND_MARKET_WIDE, BIAS_UNDERSTATES
