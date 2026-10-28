"""Phase 60 (Run 6): contracts before code.

The metric dictionary is a single source of truth: the generated document must
match the module, the label dictionary must serve every metric, and the config
schema must carry feature lineage and instrument path metadata.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from margin_engine.config import Rules, load_rules
from margin_engine.labels import LABELS
from margin_engine.metrics import METRICS, metric_label_entries, render_metric_dictionary

ROOT = Path(__file__).resolve().parents[1]


def test_metric_dictionary_doc_in_sync() -> None:
    """docs/metric-dictionary.md is generated; a stale copy fails here."""
    doc = (ROOT / "docs" / "metric-dictionary.md").read_text(encoding="utf-8")
    assert doc.replace("\r\n", "\n") == render_metric_dictionary()


def test_required_metrics_present() -> None:
    codes = {m.code for m in METRICS}
    required = {
        "list_price",
        "invoice_product_price",
        "invoice_revenue",
        "pocket_revenue",
        "pocket_gross_profit",
        "contribution",
        "contribution_after_activity",
        "observed_leakage",
        "gross_price_opportunity",
        "expected_pocket_gross_profit_change",
        "expected_contribution_change",
    }
    assert required <= codes


def test_every_metric_fully_specified() -> None:
    for m in METRICS:
        for field in (
            "label",
            "description",
            "formula",
            "units",
            "denominator",
            "price_basis",
            "includes",
            "excludes",
            "amount_status",
        ):
            assert getattr(m, field).strip(), f"{m.code}.{field} is empty"


def test_metric_entries_reach_label_dictionary() -> None:
    """UI tooltips and report labels read the label dictionary; every metric
    must be in it with the same words the generated document carries."""
    entries = metric_label_entries()
    for code, entry in entries.items():
        assert LABELS[code] == entry


def test_feature_lineage_covers_context_features() -> None:
    seg = Rules().segmentation
    for feature in seg.context_features:
        lineage = seg.feature_lineage[feature]
        assert lineage.source_columns
        assert lineage.formula
        assert lineage.reference_window


def test_feature_lineage_is_honest_about_outcome_period() -> None:
    """Phase 76 replaced the two features that carried outcome-period dollars.

    `revenue_abc` is gone from the context set (the ABC the tree sees is now
    cut on a window that ends before the line's own period) and the order-size
    band is cut on quantity. The order band is still measured over the same
    period as the target and must keep saying so; neither may claim to be
    price-free unless it is.
    """
    lineage = Rules().segmentation.feature_lineage
    assert "revenue_abc" not in Rules().segmentation.context_features
    assert not lineage["abc_class"].uses_outcome_period_data
    assert not lineage["abc_class"].embeds_realized_price
    assert "before" in lineage["abc_class"].reference_window
    # Same period, but cut on units: reported as same-period, not refused.
    assert lineage["order_size_band"].uses_outcome_period_data
    assert not lineage["order_size_band"].embeds_realized_price
    assert "net_amount_cents" not in lineage["order_size_band"].source_columns
    assert not lineage["industry"].uses_outcome_period_data


def test_missing_lineage_fails_closed() -> None:
    with pytest.raises(ValueError, match="feature_lineage"):
        Rules.model_validate({"segmentation": {"context_features": ["industry", "a_new_feature"]}})


def test_shipped_instruments_declare_paths() -> None:
    """Each instrument in the shipped rules.yaml declares its plausible paths
    to quantity, including the competitor-price path the causal gate reads."""
    rules = load_rules(ROOT / "config" / "rules.yaml")
    assert rules.sensitivity.instruments
    for inst in rules.sensitivity.instruments:
        channels = {p.channel for p in inst.paths_to_quantity}
        assert "competitor_prices" in channels, inst.name
        assert "availability" in channels, inst.name
        for p in inst.paths_to_quantity:
            assert p.treatment.strip(), f"{inst.name}:{p.channel} has no treatment"
