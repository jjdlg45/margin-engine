"""Phase 18 verification: the web bundle validates against its schema, every
figure matches the engine's frames cents-exact, explanations use only computed
numbers, output is byte-identical across runs, and the size target holds."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from pathlib import Path

import polars as pl
import pytest
import yaml

from margin_engine.config import Rules, load_rules
from margin_engine.report.full_run import FullRunResult, run_full
from margin_engine.webexport.build import build_bundle, write_bundle
from margin_engine.webexport.bundle import WebBundle

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample" / "erp"
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def _fast_rules(tmp: Path) -> Path:
    rules = yaml.safe_load((REPO / "config" / "rules.yaml").read_text(encoding="utf-8"))
    rules["segmentation"]["bootstrap_resamples"] = 2
    p = tmp / "rules.yaml"
    p.write_text(yaml.safe_dump(rules), encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[FullRunResult, Rules, WebBundle, Path]:
    tmp = tmp_path_factory.mktemp("webexport")
    rules_path = _fast_rules(tmp)
    res = run_full(SAMPLE, MAPPING, rules_path, tmp / "engine_out")
    rules = load_rules(rules_path)
    bundle = build_bundle(res, rules, REPO)
    out = tmp / "data"
    write_bundle(bundle, out)
    return res, rules, bundle, out


def test_schema_validates(built: tuple[FullRunResult, Rules, WebBundle, Path]) -> None:
    import jsonschema

    _, _, _, out = built
    payload = json.loads((out / "bundle.json").read_text(encoding="utf-8"))
    schema = json.loads((out / "schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)
    WebBundle.model_validate(payload)  # pydantic round-trip


def test_parity_kpis_and_waterfall(built: tuple[FullRunResult, Rules, WebBundle, Path]) -> None:
    """Every bundle figure equals the engine's frames, cents-exact."""
    res, _, bundle, _ = built
    sale = res.pipeline.ledger.filter(pl.col("transaction_type") == "sale")
    returns = res.pipeline.ledger.filter(pl.col("transaction_type") == "return")
    econ = res.pipeline.ledger.filter(pl.col("transaction_type").is_in(["sale", "return"]))
    cost_known = econ.filter(pl.col("product_cost_cents").is_not_null())
    assert bundle.kpis.gross_sales_cents == int(sale["net_amount_cents"].sum())
    assert bundle.kpis.returns_cents == int(returns["net_amount_cents"].sum())
    assert bundle.kpis.net_revenue_cents == int(sale["net_amount_cents"].sum()) + int(
        returns["net_amount_cents"].sum()
    )
    assert bundle.kpis.window_label.endswith("months")
    # One basis: every headline KPI is after returns, so pocket <= net.
    assert bundle.kpis.pocket_revenue_cents == int(econ["pocket_revenue_cents"].sum())
    assert bundle.kpis.pocket_revenue_cents <= bundle.kpis.net_revenue_cents
    assert bundle.kpis.pocket_gross_profit_cents == int(cost_known["pocket_gp_cents"].sum())
    assert bundle.kpis.contribution_cents == int(cost_known["contribution_cents"].sum())
    assert bundle.kpis.reconciliation_pass is True
    stages = {s.stage: s.amount_cents for s in bundle.waterfall_overall}
    assert stages["Invoice revenue"] == int(sale["invoice_revenue_cents"].sum())
    assert stages["Pocket revenue"] == int(sale["pocket_revenue_cents"].sum())
    assert stages["Gross list value"] == int(sale["gross_list_value_cents"].fill_null(0).sum())


def test_parity_group_waterfalls(built: tuple[FullRunResult, Rules, WebBundle, Path]) -> None:
    res, _, bundle, _ = built
    sale = res.pipeline.ledger.filter(pl.col("transaction_type") == "sale")
    for gw in bundle.waterfall_by_customer[:5]:
        grp = sale.filter(pl.col("customer_id") == gw.key)
        assert gw.net_revenue_cents == int(grp["net_amount_cents"].sum())
        got = {s.stage: s.amount_cents for s in gw.stages}
        assert got["Pocket revenue"] == int(grp["pocket_revenue_cents"].sum())
    seg_sale = sale.join(res.segments.leaf_assignments, on="source_line_key", how="left")
    for gw in bundle.waterfall_by_segment[:3]:
        grp = seg_sale.filter(pl.col("segment_leaf") == int(gw.key))
        assert gw.net_revenue_cents == int(grp["net_amount_cents"].sum())


def test_parity_leakage_guidance_economics(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    res, _, bundle, _ = built
    assert bundle.opportunity.leakage_total_cents == int(res.leakage["total_leakage_cents"].sum())
    assert bundle.opportunity.gross_total_cents == int(
        res.opportunities["gross_price_opportunity_cents"].sum()
    )
    assert len(bundle.guidance) == res.guidance.height
    g0 = bundle.guidance[0]
    row = res.guidance.filter(
        (pl.col("customer_id") == g0.customer_id) & (pl.col("item_id") == g0.item_id)
    ).to_dicts()[0]
    assert g0.status == row["guidance_status"]
    assert g0.pocket_floor_cents == row["pocket_floor_cents"]
    ce = res.customer_economics
    assert ce is not None
    b0 = bundle.economics.customers[0]
    crow = ce.filter(pl.col("customer_id") == b0.customer_id).to_dicts()[0]
    assert b0.contribution_cents == crow["contribution_cents"]
    assert b0.contribution_after_activity_cents == crow["contribution_after_activity_cents"]
    assert b0.net_revenue_cents == crow["net_cents"]


def test_scenario_grid_shape_and_monotonicity(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    _, _, bundle, _ = built
    assert bundle.scenario_grids, "sample should produce scenario grids"
    for grid in bundle.scenario_grids:
        # One point per 1% of pass-through, so the UI slider moves in 1%
        # steps while every displayed value still comes from the engine.
        levels = [p.pass_through for p in grid.points]
        assert levels == [k / 100 for k in range(101)]
        rec = [p.recovered_cents for p in grid.points]
        assert rec[0] == 0
        assert all(b >= a for a, b in itertools.pairwise(rec)), "recovery must not fall"
        for p in grid.points:
            assert p.incremental_cost_cents == grid.points[0].incremental_cost_cents


def test_explanations_use_only_computed_numbers(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    """Every numeric token in an explanation must come from its row's values,
    rendered with the same formatters the templates use."""
    from margin_engine.webexport.explain import _dollars, _idx

    res, _, bundle, _ = built
    gmap = {(r["customer_id"], r["item_id"]): r for r in res.guidance.to_dicts()}
    for gp in bundle.guidance[:300]:
        row = gmap[(gp.customer_id, gp.item_id)]
        allowed: set[str] = set()
        for v in row.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                allowed.update(re.findall(r"\d+(?:\.\d+)?", _idx(v)))
                allowed.update(re.findall(r"\d+(?:[,.]\d+)*", _dollars(v)))
        for tok in re.findall(r"\d+(?:[,.]\d+)*", gp.explanation):
            assert tok in allowed, f"{gp.customer_id}/{gp.item_id}: {tok!r} not from the row"
    omap = {(r["customer_id"], r["item_id"]): r for r in res.opportunities.to_dicts()}
    for op in bundle.opportunity.top_opportunities[:100]:
        row = omap[(op.customer_id, op.item_id)]
        allowed = set()
        for v in row.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                allowed.update(re.findall(r"\d+(?:[,.]\d+)*", _dollars(v)))
        for tok in re.findall(r"\d+(?:[,.]\d+)*", op.explanation):
            assert tok in allowed, f"{op.customer_id}/{op.item_id}: {tok!r} not from the row"


def test_opportunity_explanations_match_their_treatment(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    """The explanation's narrative must match the row's volume treatment -
    a causal row explains the modeled volume response, never the fixed-volume
    ceiling wording (regression: treatment codes once never matched)."""
    from margin_engine.webexport.explain import opportunity_explanation

    _, _, bundle, _ = built
    for op in bundle.opportunity.top_opportunities:
        if (
            op.volume_treatment == "expected_change"
            and op.expected_contribution_change_cents is not None
        ):
            assert "expected contribution change is" in op.explanation, op.explanation
            assert "assumes unchanged volume" not in op.explanation
        elif op.volume_treatment == "sensitivity_range_only":
            assert "sensitivity range" in op.explanation, op.explanation
    # The sample world reaches no causal rows, so the causal branch is locked
    # directly against the template (non-vacuous regardless of world size).
    text = opportunity_explanation(
        {
            "gross_price_opportunity_cents": 100_000,
            "expected_contribution_change_cents": 60_000,
            "volume_treatment": "expected_change",
            "evidence_status": "causal_elasticity_supported",
        }
    )
    assert "expected contribution change is" in text
    assert "assumes unchanged volume" not in text


def test_rule_titles_unique_per_table(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    """No two rule titles on a page may be identical (phase 23.7)."""
    _, _, bundle, _ = built
    for table in (bundle.segments.rules, bundle.segments.margin_driver_rules):
        names = [r.name for r in table]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"duplicate rule titles: {dupes}"


def test_margin_titles_use_except_phrasing() -> None:
    """One-hot exclusions phrase as 'except', grouped per feature, and paths
    differing only in a deep condition still get distinct titles."""
    from margin_engine.labels import plain_rule

    a = plain_rule(
        "NOT item_family_FAM19 AND NOT cost_ratio_band_low AND NOT item_family_FAM14 "
        "AND NOT cost_ratio_band_mid"
    )
    b = plain_rule(
        "NOT item_family_FAM19 AND NOT cost_ratio_band_low AND NOT item_family_FAM14 "
        "AND cost_ratio_band_mid"
    )
    assert "except FAM14 and FAM19" in a[0]
    assert "not " not in a[0].lower().replace("not assigned", "")
    assert a[0] != b[0]
    # Ordered bands phrase as the INCLUDED levels, never as a negation: the
    # exclusion of low and mid cost bands reads as the one remaining level.
    assert "high cost relative to price" in a[0]
    assert "cost ratio" not in a[0]


def test_ordered_bands_phrase_as_included_levels() -> None:
    """Ordered bands always read as included levels ('low or medium
    discounts'), never negations; no abbreviations or variable names reach
    the reader ('percent bands' never 'pcts', 'rarely ordered' never
    'long-tail', 'cost relative to price' never 'cost ratio')."""
    from margin_engine.labels import plain_rule

    one_hot = plain_rule("NOT discount_pct_band_high AND item_role_long_tail")
    desc = one_hot.description.lower()
    assert "low or medium discounts" in desc
    assert "rarely ordered items" in desc
    for banned in ("pct", "except", "long-tail", "long_tail"):
        assert banned not in desc, desc
    set_rule = plain_rule("(order_size_band NOT IN {large} AND revenue_abc NOT IN {C})")
    assert "small or medium orders" in set_rule.description
    assert "revenue class A or B" in set_rule.description
    assert "except" not in set_rule.description


def test_merged_segment_description_covers_every_branch() -> None:
    """A segment merged from several tree leaves must be covered in full: the
    name, the description and the condition bullets mention every branch's
    conditions, never just the first branch."""
    from margin_engine.labels import _parse_clause, _phrase, plain_rule

    rule = (
        "(item_role IN {long_tail} AND industry IN {construction, manufacturing}) "
        "OR (industry IN {oem, retail}) "
        "OR (channel IN {ecommerce} AND size_band IN {small})"
    )
    pr = plain_rule(rule)
    clauses = [c.strip(" ()") for c in rule.split(" OR ")]
    assert len(clauses) == 3
    for clause in clauses:
        parsed = _parse_clause(clause)
        assert parsed, clause
        for feature, values, negated in parsed:
            phrase = _phrase(feature, values, negated)
            assert phrase in pr.description, (
                f"branch condition {phrase!r} missing from description: {pr.description!r}"
            )
            assert any(phrase.lower() in b.lower() for b in pr.conditions), (
                f"branch condition {phrase!r} missing from bullets: {pr.conditions!r}"
            )
    # The name covers every branch too (each contributes its leading phrase).
    for lead in ("rarely ordered items", "oem or retail customers", "ecommerce channels"):
        assert lead in pr.name.lower(), pr.name
    assert "Merged from 3 tree leaves" in pr.note


def test_bundle_rules_covered_and_raw_rule_only_in_data_exports(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    """Every fitted rule's plain description covers every branch's conditions,
    every rule carries plain-English bullets, and the raw rule string stays
    out of the web bundle (it lives in the CSV/Parquet exports)."""
    from margin_engine.labels import _parse_clause, _phrase
    from margin_engine.webexport.bundle import SegmentRule

    res, _, bundle, out = built
    assert "rule" not in SegmentRule.model_fields
    assert '"rule"' not in (out / "segments.json").read_text(encoding="utf-8")
    assert "rule" in pl.DataFrame(res.segments.rules).columns  # data export keeps it
    by_leaf = {r.leaf_id: r for r in bundle.segments.rules}
    for raw in res.segments.rules:
        br = by_leaf[int(raw["leaf_id"])]
        assert br.conditions, br.name
        for clause in str(raw["rule"]).replace("(all lines)", "").split(" OR "):
            for feature, values, negated in _parse_clause(clause.strip(" ()")):
                assert _phrase(feature, values, negated) in br.plain, (
                    f"leaf {raw['leaf_id']}: branch condition missing from {br.plain!r}"
                )
    for table in (bundle.segments.rules, bundle.segments.margin_driver_rules):
        for r in table:
            assert r.conditions, f"{r.name} has no condition bullets"


def test_margin_driver_meaning_from_numbers(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    """Every margin driver carries a one-line 'what this means' summary
    generated from its own numbers; commercial segments carry none."""
    _, _, bundle, _ = built
    drivers = bundle.segments.margin_driver_rules
    assert drivers
    for r in drivers:
        assert r.meaning is not None
        assert f"{r.mean_target * 100:.1f}% pocket margin" in r.meaning
        assert "overall average" in r.meaning
    for r in bundle.segments.rules:
        assert r.meaning is None


def test_grid_levels_match_canonical_scenario_strategies(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    """Parity across the full 1% grid: the grid's 0%, 50% and 100% points
    equal the canonical run_cost_scenario strategies cents-exact, so the finer
    grid is the same math, only evaluated at more levels."""
    from datetime import date

    from margin_engine.scenarios.tariff import CostShock, run_cost_scenario

    res, rules, bundle, _ = built
    grid = bundle.scenario_grids[0]
    shock = CostShock(
        name=grid.name,
        scope=grid.scope,
        scope_value=grid.scope_value,
        pct_increase=grid.pct_increase,
        effective=date(2000, 1, 1),
    )
    assert res.panel is not None
    canon = run_cost_scenario(res.panel, res.guidance, res.sensitivity, shock, rules)
    for strategy, level in (("none", 0.0), ("partial_50", 0.5), ("full", 1.0)):
        row = canon.filter(pl.col("strategy") == strategy).to_dicts()[0]
        point = grid.points[round(level * 100)]
        assert point.pass_through == level
        assert point.incremental_cost_cents == int(row["incremental_cost_cents"])
        assert point.recovered_cents == int(row["recovered_cents"])
        assert point.residual_exposure_cents == int(row["residual_exposure_cents"])
        assert point.protected_exposure_cents == int(row["protected_exposure_cents"])


def test_opportunity_summary_states_driver(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    """The summary states the price position driving the gap, not the dollar
    figures already shown in the columns; its numbers come from the row's own
    guidance position."""
    res, _, bundle, _ = built
    gmap = {
        (g["customer_id"], g["item_id"]): g
        for g in res.guidance.select(
            "customer_id", "item_id", "current_index", "target_index"
        ).to_dicts()
    }
    checked = 0
    for op in bundle.opportunity.top_opportunities[:100]:
        g = gmap.get((op.customer_id, op.item_id))
        if g and g["current_index"] is not None and g["target_index"] is not None:
            checked += 1
            assert "below the proposed target" in op.summary, op.summary
            cur = f"{g['current_index'] * 100:.1f}%"
            assert cur in op.summary, (op.summary, cur)
            # The column dollars are not repeated in the summary.
            assert "$" not in op.summary
    assert checked > 0


def test_validation_rows_carry_no_raw_codes(
    built: tuple[FullRunResult, Rules, WebBundle, Path],
) -> None:
    """validation.json is written for maintainers; the bundle renders its text
    through the label dictionary, so no known machine code reaches the page
    raw (the deploy gate caught month_customer, persistent_demand, status
    counts and more leaking onto /methodology). Unknown codes stay raw on
    purpose so the web suite's raw-code gate fails loudly instead of the
    export prettifying them silently."""
    from margin_engine.labels import LABELS, render_text

    _, _, bundle, _ = built
    snake = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
    for row in bundle.validation_results:
        for text in (row.check, row.target, row.achieved):
            leaked = [t for t in snake.findall(text) if t in LABELS]
            assert not leaked, f"raw codes in validation row {row.check!r}: {leaked}"
    assert render_text("cluster=month_customer") == "cluster=month × customer"  # noqa: RUF001
    assert (
        render_text("statuses {causal_elasticity_supported: 8}")
        == "statuses {Causal, supported: 8}"
    )
    assert render_text("a_code_without_a_label") == "a_code_without_a_label"


def test_bundle_byte_identical_across_runs(tmp_path: Path) -> None:
    rules_path = _fast_rules(tmp_path)
    hashes = []
    for i in (1, 2):
        res = run_full(SAMPLE, MAPPING, rules_path, tmp_path / f"engine{i}")
        bundle = build_bundle(res, load_rules(rules_path), REPO)
        out = tmp_path / f"data{i}"
        write_bundle(bundle, out)
        hashes.append(
            {
                str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(out.rglob("*.json"))
            }
        )
    assert hashes[0] == hashes[1], "all exported files must be byte-identical across runs"


def test_size_under_5mb(built: tuple[FullRunResult, Rules, WebBundle, Path]) -> None:
    _, _, _, out = built
    total = sum(p.stat().st_size for p in out.rglob("*.json"))
    assert total < 5 * 1024 * 1024, f"bundle dir {total / 1e6:.1f} MB exceeds 5 MB"


def test_split_page_files(built: tuple[FullRunResult, Rules, WebBundle, Path]) -> None:
    """The split per-page / per-table files the app consumes: all present,
    pure projections of the one bundle, explanations split out, and every
    app-consumed file under the 1 MB hard limit."""
    _, _, _, out = built
    pages = [
        "app",
        "overview",
        "waterfall",
        "customers",
        "segments",
        "guidance",
        "sensitivity",
        "opportunity",
        "scenarios",
        "methodology",
        "modules",
        "tables/guidance",
        "tables/guidance_explanations",
        "tables/opportunities",
        "tables/customers",
    ]
    for name in pages:
        path = out / f"{name}.json"
        assert path.exists(), f"missing split file {name}.json"
        size = path.stat().st_size
        assert size < 1024 * 1024, f"{name}.json is {size / 1e6:.2f} MB (limit 1 MB)"

    full = json.loads((out / "bundle.json").read_text(encoding="utf-8"))
    app = json.loads((out / "app.json").read_text(encoding="utf-8"))
    assert app["labels"] == full["labels"]
    assert app["kpis"] == full["kpis"]

    rows = json.loads((out / "tables" / "guidance.json").read_text(encoding="utf-8"))["items"]
    assert rows, "guidance table must not be empty"
    assert all("explanation" not in r for r in rows), "explanations must live in their own file"
    expl = json.loads((out / "tables" / "guidance_explanations.json").read_text(encoding="utf-8"))[
        "items"
    ]
    for g in full["guidance"]:
        assert expl[f"{g['customer_id']}|{g['item_id']}"] == g["explanation"]

    customers = json.loads((out / "tables" / "customers.json").read_text(encoding="utf-8"))
    assert customers["items"] == full["economics"]["customers"]


def test_shard_table_roundtrip() -> None:
    """Sharding splits oversized table payloads into "next"-linked chunks that
    reassemble losslessly, for both list and dict items; small payloads are
    written as a single unchained file."""
    from margin_engine.webexport.build import _shard_table

    small = {"items": [{"a": 1}]}
    assert _shard_table("tables/x", small) == [("tables/x", small)]

    rows = [{"id": i, "pad": "x" * 2000} for i in range(1000)]
    shards = _shard_table("tables/big", {"items": rows})
    assert len(shards) > 1
    assert shards[0][0] == "tables/big"
    assert shards[1][0] == "tables/big_2"
    reassembled: list[dict[str, object]] = []
    for i, (_name, payload) in enumerate(shards):
        expected_next = f"tables/big_{i + 2}.json" if i + 1 < len(shards) else None
        assert payload["next"] == expected_next
        items = payload["items"]
        assert isinstance(items, list)
        reassembled.extend(items)  # type: ignore[arg-type]
        blob = json.dumps(payload, sort_keys=True, indent=1).encode("utf-8")
        assert len(blob) < 1024 * 1024
    assert reassembled == rows

    keyed = {f"K{i}": "y" * 2000 for i in range(1000)}
    dshards = _shard_table("tables/keyed", {"items": keyed})
    assert len(dshards) > 1
    merged: dict[str, str] = {}
    for _, payload in dshards:
        items_d = payload["items"]
        assert isinstance(items_d, dict)
        merged.update(items_d)  # type: ignore[arg-type]
    assert merged == keyed


def test_cli_export_web(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from margin_engine.cli import app

    rules_path = _fast_rules(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "export-web",
            "--data",
            str(SAMPLE),
            "--mapping",
            str(MAPPING),
            "--rules",
            str(rules_path),
            "--out",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "bundle.json").exists()
    assert (tmp_path / "data" / "schema.json").exists()
    assert "Guidance pairs:" in result.output
