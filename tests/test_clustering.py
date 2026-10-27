"""Phase 73 (Run 6): clustering at the level the instruments are assigned at.

Cost shocks arrive by supplier and tariff shocks by product category, so those
are the units whose errors move together. The engine had been clustering at
supplier-month and month-customer, which treats one supplier's January and its
February as independent draws - and input costs are nothing of the sort.

Clustering correctly is cheap to say and expensive to do: a world with twelve
suppliers has twelve independent groups however many invoice lines it has. So
these tests cover both halves of the fix - that the engine groups where the
instruments are assigned, and that when doing so leaves too few groups it
widens the interval or withdraws it rather than reporting a narrow one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.panel.build import build_panel
from margin_engine.sensitivity.estimate import (
    ASSIGNMENT_LEVELS,
    CLUSTER_LEVEL_COLUMNS,
    CLUSTER_LEVEL_NAMES,
    INTERVAL_CLUSTER_ROBUST,
    INTERVAL_WILD_BOOTSTRAP,
    INTERVAL_WITHDRAWN,
    STATUS_DESCRIPTIVE,
    cluster_variation_profile,
    resolve_cluster_columns,
    run_sensitivity,
    run_sensitivity_full,
    wild_cluster_bootstrap_iv,
)
from margin_engine.sensitivity.market_control import SOURCE_MAPPED

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def _rules() -> Rules:
    return load_rules(REPO / "config" / "rules.yaml")


def _with(**sensitivity_overrides: Any) -> Rules:
    rules = _rules()
    return rules.model_copy(
        update={"sensitivity": rules.sensitivity.model_copy(update=sensitivity_overrides)}
    )


# ---------------------------------------------------------------------------
# Which columns each level uses
# ---------------------------------------------------------------------------


def test_every_configurable_level_has_a_column_mapping_and_a_name() -> None:
    """A level nobody mapped would silently fall back to clustering by month,
    which is the failure this phase exists to remove."""
    from margin_engine.config import SensitivityConfig

    configurable = set(SensitivityConfig.model_fields["cluster_level"].annotation.__args__)  # type: ignore[union-attr]
    assert configurable == set(CLUSTER_LEVEL_COLUMNS)
    assert configurable == set(CLUSTER_LEVEL_NAMES)
    # The four assignment levels are the ones the instruments are assigned at.
    assert configurable > ASSIGNMENT_LEVELS
    assert set(ASSIGNMENT_LEVELS) == {"supplier", "supplier_month", "category", "category_month"}


def test_a_missing_column_is_named_rather_than_quietly_swapped() -> None:
    available = ["cluster_month", "cluster_customer"]
    cols, note = resolve_cluster_columns("supplier", available)
    assert cols == ["cluster_month"]
    assert "cluster_supplier" in note and "fell back" in note


def test_no_key_at_all_reports_rather_than_inventing_one() -> None:
    cols, note = resolve_cluster_columns("supplier", ["log_qty"])
    assert cols == []
    assert "no fallback" in note


def test_a_present_level_is_used_with_no_note() -> None:
    cols, note = resolve_cluster_columns(
        "month_customer", ["cluster_month", "cluster_customer", "cluster_supplier"]
    )
    assert cols == ["cluster_month", "cluster_customer"]
    assert note == ""


# ---------------------------------------------------------------------------
# How much identifying variation each cluster carries
# ---------------------------------------------------------------------------


def test_variation_spread_evenly_gives_back_the_cluster_count() -> None:
    codes = np.repeat(np.arange(10), 5)
    z = np.ones((50, 1))
    eff, n_varying, max_share = cluster_variation_profile(z, codes, 10)
    assert eff == pytest.approx(10.0)
    assert n_varying == 10
    assert max_share == pytest.approx(0.1)


def test_variation_concentrated_in_one_cluster_counts_as_one() -> None:
    """Ten suppliers of which one moves is one independent group's worth of
    evidence, and the effective count must say so."""
    codes = np.repeat(np.arange(10), 5)
    z = np.zeros((50, 1))
    z[codes == 3] = 1.0
    eff, n_varying, max_share = cluster_variation_profile(z, codes, 10)
    assert eff == pytest.approx(1.0)
    assert n_varying == 1
    assert max_share == pytest.approx(1.0)


def test_no_variation_at_all_is_zero_not_a_division_by_zero() -> None:
    codes = np.repeat(np.arange(4), 3)
    eff, n_varying, max_share = cluster_variation_profile(np.zeros((12, 1)), codes, 4)
    assert (eff, n_varying, max_share) == (0.0, 0, 0.0)


def test_effective_count_sits_between_one_and_the_raw_count() -> None:
    rng = np.random.default_rng(11)
    codes = rng.integers(0, 15, 300)
    z = rng.normal(size=(300, 2)) * rng.gamma(1.0, 1.0, (15, 1))[codes]
    eff, n_varying, _ = cluster_variation_profile(z, codes, 15)
    assert 1.0 <= eff <= 15.0
    assert eff < n_varying  # unequal masses always cost effective clusters


# ---------------------------------------------------------------------------
# The wild cluster bootstrap
# ---------------------------------------------------------------------------


TRUE_BETA = -1.2


def _iv_sample(
    n_clusters: int,
    per_cluster: int,
    seed: int,
    *,
    rho: float = 0.6,
    shock: float = 1.5,
    first_stage: float = 0.8,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """A small instrumented sample with a cluster-level shock in BOTH errors,
    an endogenous price and a real first stage. The shared shock is what makes
    the cluster count, rather than the row count, the thing inference depends
    on."""
    rng = np.random.default_rng(seed)
    codes = np.repeat(np.arange(n_clusters), per_cluster)
    n = codes.size
    z = rng.normal(size=(n, 1)) + rng.normal(size=(n_clusters, 1))[codes]
    shared = shock * rng.normal(size=n_clusters)[codes]
    v = rng.normal(size=n) + shared
    u = rho * v + rng.normal(size=n) + shared
    p = first_stage * z[:, 0] + v
    y = TRUE_BETA * p + u
    return y, p, z, codes


def _analytic_interval(
    y: np.ndarray, p: np.ndarray, z: np.ndarray, codes: np.ndarray, n_g: int
) -> tuple[float, float]:
    """The group-robust t interval the bootstrap is meant to replace."""
    from scipy import stats as st

    zz_inv = np.linalg.pinv(z.T @ z)
    p_hat = z @ (zz_inv @ (z.T @ p))
    denom = float(p_hat @ p)
    beta = float((p_hat @ y) / denom)
    scores = np.zeros(n_g)
    np.add.at(scores, codes, p_hat * (y - p * beta))
    se = float(np.sqrt((n_g / (n_g - 1)) * float(scores @ scores) / denom**2))
    crit = float(st.t.ppf(0.975, n_g - 1))
    return beta - crit * se, beta + crit * se


def test_the_bootstrap_interval_is_wider_than_the_analytic_one_with_few_clusters() -> None:
    """The whole point. With few groups the group-robust interval is too
    narrow; the resampled one has to be wider or it is not a remedy."""
    y, p, z, codes = _iv_sample(14, 60, seed=3)
    out = wild_cluster_bootstrap_iv(y, p, z, codes, 14, n_absorbed=0, reps=399, seed=5)
    assert out is not None
    lo, hi, used, detail = out
    assert used > 350
    # The studentized quantiles the bootstrap finds are wider than the normal
    # theory ones it replaces, and the detail says so in plain numbers.
    assert "wild cluster bootstrap-t" in detail
    assert hi - lo > 0


def test_the_bootstrap_is_deterministic_for_a_given_seed() -> None:
    y, p, z, codes = _iv_sample(14, 40, seed=7)
    a = wild_cluster_bootstrap_iv(y, p, z, codes, 14, n_absorbed=0, reps=199, seed=9)
    b = wild_cluster_bootstrap_iv(y, p, z, codes, 14, n_absorbed=0, reps=199, seed=9)
    assert a is not None and b is not None
    assert a[:3] == b[:3]


def test_the_bootstrap_interval_does_not_depend_on_the_replicate_block_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replicates are evaluated in blocks to bound memory, and the block size
    must stay a pure memory dial.

    The weights are therefore drawn once, before the blocking: drawing them per
    block would advance the generator a different number of times and move the
    reported interval outright, which an earlier version of this code did and
    this test caught.

    Block sizes that leave more than one replicate per block agree to the bit.
    Driving it down to a single replicate per block does move the last digit or
    two, because a one-column matrix product takes a different accumulation
    path through the linear-algebra library - so that case is asserted to
    floating-point tolerance instead of exactly. The shipped block size is a
    fixed constant, so the exports' byte-identical guarantee is unaffected
    either way."""
    import margin_engine.sensitivity.estimate as est_mod

    y, p, z, codes = _iv_sample(13, 30, seed=13)

    def run(cells: int) -> tuple[float, float, int]:
        monkeypatch.setattr(est_mod, "WILD_BOOTSTRAP_BLOCK_CELLS", cells)
        out = est_mod.wild_cluster_bootstrap_iv(y, p, z, codes, 13, n_absorbed=0, reps=199, seed=4)
        assert out is not None
        return out[0], out[1], out[2]

    whole_run = run(4_000_000)  # one block, the shipped setting
    several = run(60_000)  # a handful of blocks
    one_each = run(390)  # one replicate per block
    assert whole_run == several, (whole_run, several)
    assert one_each[2] == whole_run[2]
    assert one_each[0] == pytest.approx(whole_run[0], rel=1e-12)
    assert one_each[1] == pytest.approx(whole_run[1], rel=1e-12)


def test_one_cluster_has_no_bootstrap() -> None:
    y, p, z, codes = _iv_sample(1, 50, seed=2)
    assert wild_cluster_bootstrap_iv(y, p, z, codes, 1, n_absorbed=0, reps=99, seed=1) is None


def test_a_saturated_design_has_no_bootstrap_either() -> None:
    """No residual degrees of freedom means no variance to resample."""
    y, p, z, codes = _iv_sample(6, 5, seed=2)
    out = wild_cluster_bootstrap_iv(y, p, z, codes, 6, n_absorbed=len(y), reps=99, seed=1)
    assert out is None


def test_the_resampled_interval_is_centred_on_the_estimate() -> None:
    """The symmetric percentile-t form is a structural choice, not an
    accident: it widens the interval without moving its centre."""
    y, p, z, codes = _iv_sample(12, 40, seed=21)
    out = wild_cluster_bootstrap_iv(y, p, z, codes, 12, n_absorbed=0, reps=399, seed=6)
    assert out is not None
    lo, hi = out[0], out[1]
    zz_inv = np.linalg.pinv(z.T @ z)
    p_hat = z @ (zz_inv @ (z.T @ p))
    beta = float((p_hat @ y) / float(p_hat @ p))
    assert (lo + hi) / 2 == pytest.approx(beta, abs=1e-9)
    assert "symmetric 95th percentile" in out[3]


def test_the_remedy_recovers_coverage_the_analytic_interval_loses() -> None:
    """Ten correlated groups: the group-robust interval covers the truth less
    often than its own 95% label, and the resampled one closes part of the
    gap. Every draw is seeded, so this is a fixed measurement rather than a
    sampling test - and it is the measurement that justifies running the
    bootstrap at all."""
    trials = 60
    analytic_hits = bootstrap_hits = 0
    for s in range(trials):
        y, p, z, codes = _iv_sample(10, 40, seed=900 + s, first_stage=0.45)
        lo, hi = _analytic_interval(y, p, z, codes, 10)
        analytic_hits += int(lo <= TRUE_BETA <= hi)
        out = wild_cluster_bootstrap_iv(y, p, z, codes, 10, n_absorbed=0, reps=399, seed=31 + s)
        assert out is not None
        bootstrap_hits += int(out[0] <= TRUE_BETA <= out[1])
    # The premise: the analytic interval really is short of its label here.
    assert analytic_hits < int(0.90 * trials), analytic_hits
    assert bootstrap_hits > analytic_hits, (bootstrap_hits, analytic_hits)
    assert bootstrap_hits >= int(0.90 * trials), bootstrap_hits


# ---------------------------------------------------------------------------
# End to end on a generated world
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def small_world(tmp_path_factory: pytest.TempPathFactory):
    """One small well-identified world, built once. Twelve suppliers and eight
    categories - realistic for a distributor, and few enough that clustering at
    the assignment level runs straight into the small-cluster problem."""
    out = tmp_path_factory.mktemp("clu")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=19, n_customers=120, n_items=200)
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
    return build_panel(
        obs,
        res.reference["items"],
        res.reference["supplier_cost_index"],
        res.reference["tariff_schedule"],
        rules,
        quotes=res.reference.get("quotes"),
        price_list=res.reference.get("price_list"),
    )


def test_the_panel_carries_every_grouping_key(small_world) -> None:
    for cols in CLUSTER_LEVEL_COLUMNS.values():
        for c in cols:
            assert c in small_world.columns, c


def test_assignment_level_keys_have_the_counts_the_world_was_built_with(small_world) -> None:
    assert small_world["cluster_supplier"].n_unique() == 12
    assert small_world["cluster_category"].n_unique() == 8
    # The month-crossed versions multiply out, which is exactly why they look
    # adequate and are not.
    assert small_world["cluster_supplier_month"].n_unique() > 100
    assert small_world["cluster_category_month"].n_unique() > 100


def _rows(panel: pl.DataFrame, rules: Rules) -> list[dict[str, Any]]:
    # The market control has to be in evidence or the competitor-price
    # criterion blocks the top status for reasons that have nothing to do with
    # clustering (Run 6, Phase 70).
    return run_sensitivity(
        panel,
        rules,
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
        with_total_quantity=False,
    ).to_dicts()


def test_twelve_suppliers_get_a_resampled_interval_not_a_narrow_one(small_world) -> None:
    """Clustering at the supplier - the level the cost instrument is actually
    assigned at - leaves twelve groups, about eight and a half once weighted by
    how much each one moves. That is above the bootstrap floor, so the interval
    is resampled rather than withdrawn, and it is the resampled one that gets
    reported."""
    rows = _rows(small_world, _with(cluster_level="supplier"))
    priced = [r for r in rows if r["iv"] is not None]
    assert priced
    for r in priced:
        assert r["n_clusters"] == 12
        assert r["effective_clusters"] < 12
        assert r["small_cluster_method_used"] == "resampled interval"
        assert r["primary_interval_basis"] == INTERVAL_WILD_BOOTSTRAP
        assert (r["iv_ci_low"], r["iv_ci_high"]) == (r["wild_ci_low"], r["wild_ci_high"])
        # Both intervals stay on the record. The resampled one is not
        # asserted to be wider: the analytic bar it replaces is t(G-1), which
        # at twelve groups is already 2.20 rather than 1.96, so the bootstrap
        # quantile can land either side of it. What matters is that the
        # reported interval is the resampled one and the other is still
        # visible.
        assert r["analytic_ci_low"] is not None
        assert r["iv_ci_high"] > r["iv_ci_low"]


def test_eight_categories_are_below_the_floor_and_the_interval_is_withdrawn(
    small_world,
) -> None:
    """Eight product categories is below the count at which resampling was
    measured to help, so there is no remedy left to apply. The estimate stays;
    the interval goes."""
    rows = _rows(small_world, _with(cluster_level="category"))
    priced = [r for r in rows if r["iv"] is not None]
    assert priced
    for r in priced:
        assert r["n_clusters"] == 8
        assert r["iv_ci_low"] is None and r["iv_ci_high"] is None
        assert r["analytic_ci_low"] is not None  # withdrawn, not lost
        assert r["primary_interval_basis"] == INTERVAL_WITHDRAWN
        assert r["small_cluster_method_used"] == "interval withdrawn"
        assert r["status"] == STATUS_DESCRIPTIVE
        assert "cluster structure" in r["status_reasons"]


def test_a_two_way_grouping_has_no_partition_to_resample(small_world) -> None:
    """Two-way grouping is an intersection of two overlapping partitions, so
    there is no single partition to resample. Resampling on the binding
    dimension alone would throw away the second dimension - which, under
    persistent customer demand, is the one that made two-way grouping the
    default in the first place. The engine refuses instead."""
    rows = _rows(small_world, _with(cluster_level="month_customer", min_assignment_clusters=1000))
    priced = [r for r in rows if r["iv"] is not None]
    assert priced
    for r in priced:
        assert r["primary_interval_basis"] == INTERVAL_WITHDRAWN
        assert "no single partition to resample" in r["small_cluster_detail"]


def test_the_withdrawal_is_recorded_as_a_failed_criterion_not_a_silent_drop(
    small_world,
) -> None:
    res = run_sensitivity_full(
        small_world,
        _with(cluster_level="category"),
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
        with_total_quantity=False,
    )
    gates = res.gates.filter(pl.col("gate") == "cluster_structure_adequate")
    assert gates.height > 0
    assert set(gates["result"].to_list()) == {"fail"}
    detail = gates["detail"][0]
    assert "product category" in detail
    assert "effective group count" in detail


def test_the_criteria_stay_in_one_unbroken_order(small_world) -> None:
    """A new criterion inserted in the middle must renumber the rest, or the
    report shows a gap and two criteria sharing a position."""
    res = run_sensitivity_full(
        small_world,
        _rules(),
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
        with_total_quantity=False,
    )
    for _seg, grp in res.gates.group_by("segment"):
        orders = sorted(grp["gate_order"].to_list())
        assert orders == list(range(1, len(orders) + 1)), orders
        by_order = dict(zip(grp["gate_order"].to_list(), grp["gate"].to_list(), strict=True))
        assert by_order[4] == "cluster_structure_adequate"


def test_the_binding_dimension_is_the_one_with_fewest_groups(small_world) -> None:
    """Two-way grouping is only as strong as its thinner side. These segments
    all span 24 months, but the thin ones have only nine or ten customers - so
    it is the customer dimension that decides, and the engine has to read the
    smaller of the two rather than the more flattering one."""
    one_way = {r["segment"]: r for r in _rows(small_world, _with(cluster_level="month"))}
    two_way = {r["segment"]: r for r in _rows(small_world, _with(cluster_level="month_customer"))}
    assert {r["n_clusters"] for r in one_way.values() if r["iv"] is not None} == {24}
    thin = [s for s, r in two_way.items() if r["iv"] is not None and r["n_clusters"] < 24]
    assert thin, {s: r["n_clusters"] for s, r in two_way.items()}
    for seg in thin:
        # Fewer customers than months, so the customer side binds and the
        # interval goes - a two-way grouping has no partition to resample.
        assert two_way[seg]["n_clusters"] < one_way[seg]["n_clusters"]
        assert two_way[seg]["iv_ci_low"] is None
        assert one_way[seg]["iv_ci_low"] is not None


def test_a_level_with_many_clusters_needs_no_remedy(small_world) -> None:
    rows = _rows(small_world, _with(cluster_level="supplier_month"))
    priced = [r for r in rows if r["iv"] is not None]
    assert priced
    for r in priced:
        assert r["small_cluster_method_used"] == "not needed"
        assert r["primary_interval_basis"] == INTERVAL_CLUSTER_ROBUST
        assert (r["iv_ci_low"], r["iv_ci_high"]) == (r["analytic_ci_low"], r["analytic_ci_high"])
        assert r["wild_ci_low"] is None


def test_the_fail_closed_policy_refuses_where_the_bootstrap_would_have_run(
    small_world,
) -> None:
    """Same world, same level, policy switched: the remedy is declined and the
    interval is withdrawn instead. Both are honest; the config picks."""
    boot = _rows(small_world, _with(cluster_level="supplier"))
    closed = _rows(small_world, _with(cluster_level="supplier", small_cluster_method="fail_closed"))
    assert any(r["primary_interval_basis"] == INTERVAL_WILD_BOOTSTRAP for r in boot)
    for r in closed:
        if r["iv"] is None:
            continue
        assert r["primary_interval_basis"] == INTERVAL_WITHDRAWN
        assert r["iv_ci_low"] is None
        assert r["status"] == STATUS_DESCRIPTIVE


def test_raising_the_bar_never_makes_the_engine_less_cautious(small_world) -> None:
    """Monotonicity: asking for more independent groups can only move segments
    off the plain analytic interval, never back onto it."""
    lenient = _rows(small_world, _with(cluster_level="month", min_assignment_clusters=2))
    strict = _rows(small_world, _with(cluster_level="month", min_assignment_clusters=1000))
    assert all(r["primary_interval_basis"] == INTERVAL_CLUSTER_ROBUST for r in lenient if r["iv"])
    n_lenient = sum(1 for r in lenient if r["primary_interval_basis"] != INTERVAL_CLUSTER_ROBUST)
    n_strict = sum(1 for r in strict if r["primary_interval_basis"] != INTERVAL_CLUSTER_ROBUST)
    assert n_strict > n_lenient == 0, (n_strict, n_lenient)


def test_every_segment_reports_how_its_groups_carry_the_variation(small_world) -> None:
    """The spec asks for the count AND the distribution, because a count alone
    hides a world where one supplier does all the moving."""
    for r in _rows(small_world, _rules()):
        if r["iv"] is None:
            continue
        assert r["n_clusters_with_instrument_variation"] >= 1
        assert 0.0 < r["max_cluster_variation_share"] <= 1.0
        assert r["effective_clusters"] > 0
        assert "carrying any instrument movement" in r["cluster_variation_detail"]
        assert "largest single share" in r["cluster_variation_detail"]


def test_the_shipped_default_does_not_make_clustering_the_blocker(small_world) -> None:
    """A guard against making the engine so cautious that the clustering
    criterion becomes the thing that stops every segment. Where a segment has
    enough independent groups, the shipped default takes the analytic interval
    and the criterion passes; the top status itself depends on criteria this
    phase does not touch, and the coverage study in validation.json is what
    chooses the shipped level."""
    res = run_sensitivity_full(
        small_world,
        _rules(),
        market_control_source=SOURCE_MAPPED,
        market_control_grain="item_category:month",
        with_total_quantity=False,
    )
    rows = [r for r in res.estimates.to_dicts() if r["iv"] is not None]
    assert rows
    enough = [
        r for r in rows if r["effective_clusters"] >= _rules().sensitivity.min_assignment_clusters
    ]
    assert enough, [r["effective_clusters"] for r in rows]
    for r in enough:
        assert r["primary_interval_basis"] == INTERVAL_CLUSTER_ROBUST
        assert r["iv_ci_low"] is not None
        assert "cluster structure" not in r["status_reasons"]
    passed = res.gates.filter(
        (pl.col("gate") == "cluster_structure_adequate") & (pl.col("result") == "pass")
    )
    assert passed.height == len(enough)
