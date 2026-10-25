"""Phase 46: display casing for categorical data values - capitalized words,
acronyms from the mapping fully upper-case, raw values untouched in exports."""

from __future__ import annotations

from pathlib import Path

from margin_engine.ingest.mapping import load_mapping
from margin_engine.labels import display_value, plain_rule

REPO = Path(__file__).resolve().parents[1]


def test_display_value_casing_and_acronyms() -> None:
    acr = frozenset({"OEM", "HVAC"})
    assert display_value("construction") == "Construction"
    assert display_value("oem", acr) == "OEM"
    assert display_value("oem") == "Oem"  # no acronym declared: plain casing
    assert display_value("hvac_parts", acr) == "HVAC Parts"
    assert display_value("north_east") == "North East"
    assert display_value("") == ""


def test_plain_rule_uses_display_casing() -> None:
    rule = "industry IN {construction, oem} AND region IN {west}"
    r = plain_rule(rule, frozenset({"OEM"}))
    assert r.name == "Construction or OEM customers with West regions"
    assert "Construction or OEM customers" in r.conditions[0]
    assert "West regions" in r.conditions[1]
    # Engine-constructed descriptor words keep their phrasing.
    kvi = plain_rule("item_role IN {kvi}")
    assert "Key-value" in kvi.name


def test_mapping_declares_acronyms() -> None:
    m = load_mapping(REPO / "config" / "mappings" / "synth_erp.yaml")
    assert "OEM" in m.display.acronyms
    m2 = load_mapping(REPO / "config" / "mappings" / "online_retail_ii.yaml")
    assert "UK" in m2.display.acronyms
