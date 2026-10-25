"""Phase 43: drift monitoring — planted shifts fire the right flags with
plain-English messages; moves inside the thresholds stay quiet; the store
baselines and serves flags; the API exposes them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from margin_engine.config import load_rules
from margin_engine.drift import DriftStore, compare_runs
from margin_engine.report.full_run import FullRunResult, run_full

REPO = Path(__file__).resolve().parents[1]
RULES = load_rules(REPO / "config" / "rules.yaml")


def _snap(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "data_quality": {
            "missing_cost_revenue_share": 0.05,
            "data_quality_anomaly_share": 0.01,
            "commercial_outlier_share": 0.02,
            "reconciliation_pass": True,
        },
        "sensitivity": [
            {"segment": 1, "iv": -1.4, "status": "causal_elasticity_supported"},
            {"segment": 2, "iv": -1.4, "status": "iv_estimated_elasticity"},
        ],
        "guidance_status_counts": {"in_band": 100, "below_floor": 20},
        "n_ledger_lines": 1000,
    }
    base.update(over)
    return base


def test_elasticity_shift_flags_with_plain_sentence() -> None:
    prev = _snap()
    curr = _snap(
        sensitivity=[
            {"segment": 1, "iv": -1.4, "status": "causal_elasticity_supported"},
            {"segment": 2, "iv": -0.9, "status": "iv_estimated_elasticity"},
        ]
    )
    res = compare_runs(prev, curr, None, None, RULES)
    msgs = [f["message"] for f in res.flags]
    assert any(
        m == "Segment 2's price sensitivity moved from -1.40 to -0.90 since the previous refresh."
        for m in msgs
    ), msgs
    # Segment 1 did not move: no flag for it.
    assert not any(f["code"] == "sensitivity:elasticity:1" for f in res.flags)
    # A move INSIDE the threshold stays quiet.
    small = _snap(
        sensitivity=[
            {"segment": 1, "iv": -1.4, "status": "causal_elasticity_supported"},
            {"segment": 2, "iv": -1.2, "status": "iv_estimated_elasticity"},
        ]
    )
    assert compare_runs(prev, small, None, None, RULES).flags == []


def test_status_change_flag_uses_labels() -> None:
    curr = _snap(
        sensitivity=[
            {"segment": 1, "iv": -1.4, "status": "iv_estimated_elasticity"},
            {"segment": 2, "iv": -1.4, "status": "iv_estimated_elasticity"},
        ]
    )
    res = compare_runs(_snap(), curr, None, None, RULES)
    flag = next(f for f in res.flags if f["code"] == "sensitivity:status:1")
    assert '"Causal, supported"' in flag["message"]
    assert '"Instrumented estimate"' in flag["message"]
    assert "causal_elasticity_supported" not in flag["message"]  # no raw codes


def test_segment_ari_flag_and_min_lines_gate() -> None:
    keys = [f"L{i:04d}" for i in range(400)]
    prev_a = pl.DataFrame({"source_line_key": keys, "segment_leaf": [i % 4 for i in range(400)]})
    same = compare_runs(_snap(), _snap(), prev_a, prev_a, RULES)
    assert not any(f["code"] == "segments:ari" for f in same.flags)
    assert same.detail["segment_ari"]["ari"] == 1.0
    # A genuinely different partition (label permutations are agreement to
    # ARI): the four groups collapse into two -> low agreement fires.
    shuffled = prev_a.with_columns((pl.col("segment_leaf") % 2).alias("segment_leaf"))
    moved = compare_runs(_snap(), _snap(), prev_a, shuffled, RULES)
    flag = next(f for f in moved.flags if f["code"] == "segments:ari")
    assert "agreement with the previous refresh" in flag["message"]
    assert "400" in flag["message"]
    # Too few shared lines: no comparison, stated in the detail.
    tiny = prev_a.head(10)
    gated = compare_runs(_snap(), _snap(), tiny, tiny, RULES)
    assert not any(f["code"] == "segments:ari" for f in gated.flags)
    assert gated.detail["segment_ari"]["ari"] is None


def test_data_quality_rate_threshold_boundary() -> None:
    # Exactly at the 2.0pp threshold: quiet (strictly greater fires).
    at = _snap()
    at["data_quality"] = dict(at["data_quality"], missing_cost_revenue_share=0.07)
    assert not compare_runs(_snap(), at, None, None, RULES).flags
    over = _snap()
    over["data_quality"] = dict(over["data_quality"], missing_cost_revenue_share=0.075)
    res = compare_runs(_snap(), over, None, None, RULES)
    flag = next(f for f in res.flags if f["code"] == "data_quality:missing_cost_revenue_share")
    assert "rose from 5.0% to 7.5%" in flag["message"]
    # Reconciliation regression is always a flag.
    broken = _snap()
    broken["data_quality"] = dict(broken["data_quality"], reconciliation_pass=False)
    res2 = compare_runs(_snap(), broken, None, None, RULES)
    assert any(f["code"] == "data_quality:reconciliation" for f in res2.flags)


def test_status_count_change_needs_share_and_absolute_floor() -> None:
    # +4 pairs on a 20-pair status: 20% share but under the absolute floor.
    small = _snap(guidance_status_counts={"in_band": 100, "below_floor": 24})
    assert not compare_runs(_snap(), small, None, None, RULES).flags
    big = _snap(guidance_status_counts={"in_band": 100, "below_floor": 31})
    res = compare_runs(_snap(), big, None, None, RULES)
    flag = next(f for f in res.flags if f["code"] == "guidance:status:below_floor")
    assert "rose from 20 to 31" in flag["message"]
    assert "Below floor" in flag["message"]  # label, not the raw code


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> FullRunResult:
    return run_full(
        REPO / "data" / "sample" / "erp",
        REPO / "config" / "mappings" / "synth_erp.yaml",
        REPO / "config" / "rules.yaml",
        tmp_path_factory.mktemp("out"),
        validate=False,
    )


def test_store_baselines_then_flags_planted_shift(
    sample_run: FullRunResult, tmp_path: Path
) -> None:
    store = DriftStore(tmp_path / "drift")
    first = store.refresh(sample_run, RULES)
    assert first.flags == []  # no previous run: nothing to compare
    # Identical run: a comparison ran and found nothing.
    second = store.refresh(sample_run, RULES)
    assert second.flags == []
    assert store.latest()["detail"]["segment_ari"]["ari"] == 1.0
    # Plant a shift into the stored baseline, then refresh with the same run.
    snap_path = tmp_path / "drift" / "previous_snapshot.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    planted_seg = int(snap["sensitivity"][0]["segment"])
    snap["sensitivity"][0]["iv"] = (snap["sensitivity"][0]["iv"] or 0.0) + 1.0
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    third = store.refresh(sample_run, RULES)
    assert any(f["code"] == f"sensitivity:elasticity:{planted_seg}" for f in third.flags)
    assert store.latest()["flags"] == third.flags


def test_api_serves_drift_flags(tmp_path: Path) -> None:
    from api.main import create_app
    from api.settings import Settings
    from fastapi.testclient import TestClient

    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    (drift_dir / "latest_flags.json").write_text(
        json.dumps(
            {
                "flags": [
                    {
                        "code": "sensitivity:elasticity:2",
                        "message": (
                            "Segment 2's price sensitivity moved from -1.40 to -0.90 "
                            "since the previous refresh."
                        ),
                    }
                ],
                "detail": {"segment_ari": {"ari": 0.95, "n_common_lines": 5000}},
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        bundle_dir=tmp_path / "nobundle",
        db_path=tmp_path / "api.duckdb",
        actions_db=tmp_path / "actions.duckdb",
        drift_dir=drift_dir,
        fx_refresh_s=10_000,
        ppi_refresh_s=10_000,
    )
    with TestClient(create_app(settings)) as client:
        d = client.get("/api/drift").json()
        assert d["flags"][0]["code"] == "sensitivity:elasticity:2"
        assert "moved from -1.40 to -0.90" in d["flags"][0]["message"]
        # /api/app needs a loaded bundle; drift itself must not.
