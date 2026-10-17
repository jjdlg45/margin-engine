"""Phase 18 verification: the web bundle validates against its schema, every
figure matches the engine's frames cents-exact, explanations use only computed
numbers, output is byte-identical across runs, and the size target holds."""

from __future__ import annotations

import hashlib
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
    cost_known = sale.filter(pl.col("product_cost_cents").is_not_null())
    assert bundle.kpis.net_revenue_cents == int(sale["net_amount_cents"].sum())
    assert bundle.kpis.pocket_revenue_cents == int(sale["pocket_revenue_cents"].sum())
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
        levels = [p.pass_through for p in grid.points]
        assert levels == [round(k / 10, 1) for k in range(11)]
        rec = [p.recovered_cents for p in grid.points]
        assert rec[0] == 0
        assert all(b >= a for a, b in zip(rec, rec[1:], strict=False)), "recovery must not fall"
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


def test_bundle_byte_identical_across_runs(tmp_path: Path) -> None:
    rules_path = _fast_rules(tmp_path)
    hashes = []
    for i in (1, 2):
        res = run_full(SAMPLE, MAPPING, rules_path, tmp_path / f"engine{i}")
        bundle = build_bundle(res, load_rules(rules_path), REPO)
        out = tmp_path / f"data{i}"
        bundle_path, schema_path = write_bundle(bundle, out)
        hashes.append(
            (
                hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
                hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            )
        )
    assert hashes[0] == hashes[1], "bundle must be byte-identical across runs"


def test_size_under_5mb(built: tuple[FullRunResult, Rules, WebBundle, Path]) -> None:
    _, _, _, out = built
    total = sum(p.stat().st_size for p in out.iterdir())
    assert total < 5 * 1024 * 1024, f"bundle dir {total / 1e6:.1f} MB exceeds 5 MB"


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
