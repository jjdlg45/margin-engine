"""Phase 15 verification: planted KVIs in top candidates (>=80%), corridor
tightening labeled, candidates labeled as scores."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.dispersion.analytics import run_dispersion
from margin_engine.extended.kvi import kvi_candidate_scores
from margin_engine.guidance.bands import build_guidance
from margin_engine.ledger.pipeline import run_pipeline

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"


def test_planted_kvis_in_top_candidates(tmp_path: Path) -> None:
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=12, n_customers=100, n_items=250)
    generate(cfg, tmp_path)
    rules = load_rules(REPO / "config" / "rules.yaml")
    res = run_pipeline(tmp_path / "erp", MAPPING, rules)
    flagged, _, _ = run_dispersion(res.observations, res.reference["items"], rules)
    scores = kvi_candidate_scores(flagged, rules)
    planted = set(res.reference["items"].filter(pl.col("item_role") == "kvi")["item_id"].to_list())
    assert planted
    candidates = set(scores.filter(pl.col("is_kvi_candidate"))["item_id"].to_list())
    recall = len(planted & candidates) / len(planted)
    print(f"\nKVI candidate recall: {recall:.2%} ({len(planted)} planted)")
    assert recall >= 0.80, f"only {recall:.2%} of planted KVIs in top candidates"
    assert (scores["kvi_label"] == "candidate_score_not_decision").all()


def test_kvi_corridor_tightened_and_labeled() -> None:
    rules = Rules.model_validate({"price_guidance": {"min_band_observations": 1}})
    n = 30
    df = pl.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(n)] + ["CK", "CN"],
            "item_id": ["I1"] * n + ["IKVI", "INORM"],
            "segment_leaf": [1] * (n + 2),
            "price_index": [0.80 + 0.01 * (i % 21) for i in range(n)] + [0.90, 0.90],
            "invoice_date": pl.Series([date(2024, 1, 15)] * (n + 2), dtype=pl.Date),
            "quantity_norm": [1.0] * (n + 2),
            "product_cost_cents": [5_000] * (n + 2),
            "eff_list_cents": [10_000] * (n + 2),
            "contract_id": [None] * (n + 2),
            "flag_data_quality_anomaly": [False] * (n + 2),
            "item_role": ["core"] * n + ["kvi", "core"],
        }
    )
    result = build_guidance(df, rules, None)
    rows = {r["customer_id"]: r for r in result.guidance.to_dicts()}
    kvi, norm = rows["CK"], rows["CN"]
    assert kvi["corridor_policy"] == "kvi_tightened_corridor"
    assert norm["corridor_policy"] is None
    # Tightened: KVI band strictly inside the normal band.
    assert kvi["band_p25"] > norm["band_p25"]
    assert kvi["band_p75"] < norm["band_p75"]
    # Both centered on the same median.
    assert kvi["band_p50"] == norm["band_p50"]
