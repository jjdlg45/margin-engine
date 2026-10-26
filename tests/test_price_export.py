"""Phase 40: ERP-ready price export.

Only approved price actions export; template variants format correctly; the
diff summary never sums across impact bases; exporting transitions and logs;
re-exporting is idempotent; and the file round-trips through the engine's own
ingest with prices matching to the cent.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from margin_engine.actions.export import build_price_file, diff_summary, export_price_file
from margin_engine.actions.generate import ACTIONS_SCHEMA
from margin_engine.actions.model import EVENTS_SCHEMA, replay_status
from margin_engine.actions.store import ActionStore
from margin_engine.config import PriceExportConfig, load_rules

REPO = Path(__file__).resolve().parents[1]
RULES = load_rules(REPO / "config" / "rules.yaml")
AT = dt.datetime(2026, 1, 26, tzinfo=dt.UTC)


def _action(aid: str, atype: str = "price_change", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "action_id": aid,
        "action_type": atype,
        "title": f"t {aid}",
        "rationale": "r",
        "customer_id": "C1",
        "item_id": f"I_{aid[-1]}",
        "scope": "customer_item",
        "uom": "EA",
        "segment_leaf": 1,
        "guidance_status": "below_reference",
        "evidence_status": "descriptive_elasticity",
        "volume_treatment": "fixed_volume_only",
        "predicted_volume_ratio": 1.0,
        "impact_basis": "gross_price_opportunity_fixed_volume",
        "expected_impact_cents": 10_000,
        "current_price_cents": 1_000,
        "proposed_price_cents": 1_250,
        "floor_price_cents": 900,
        "pass_through_level": None,
        "incremental_cost_cents": None,
        "scope_key": None,
        "baseline_period": None,
        "intervention": None,
        "affected_set": None,
        "scenario_id": None,
        "overlap_group": None,
        "input_fingerprint": None,
        "config_fingerprint": None,
        "model_fingerprint": None,
        "expires_on": None,
        "priority": "medium",
        "owner": "unassigned",
        "due_date": dt.date(2026, 2, 25),
        "compliance_flags": "",
        "blocked": False,
        "blocked_reason": None,
        "created_at": AT,
    }
    row.update(over)
    return row


def _seeded_store(tmp_path: Path) -> ActionStore:
    actions = pl.DataFrame(
        [
            _action("act_a1"),  # will be approved
            _action("act_a2", impact_basis="expected_contribution_change"),  # approved
            _action("act_a3"),  # stays proposed
            _action("act_a4", atype="below_floor_fix", proposed_price_cents=900),  # approved
            _action(
                "act_a5", atype="service_policy", proposed_price_cents=None, uom=None
            ),  # approved but not a price action
        ]
    ).cast(ACTIONS_SCHEMA)
    events = pl.DataFrame(
        [
            {
                "action_id": aid,
                "seq": 0,
                "who": "engine",
                "at": AT,
                "from_status": None,
                "to_status": "proposed",
                "note": "",
            }
            for aid in actions["action_id"].to_list()
        ],
        schema=EVENTS_SCHEMA,
    )
    store = ActionStore(tmp_path / "actions.duckdb")
    store.seed(actions, events)
    for aid in ("act_a1", "act_a2", "act_a4", "act_a5"):
        store.transition(aid, who="demo-analyst", to_status="under_review", at=AT)
        store.transition(aid, who="demo-approver", to_status="approved", note="ok", at=AT)
    return store


def test_only_approved_price_actions_export(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    csv_text, rows = build_price_file(store.actions(), store.events(), RULES.actions.price_export)
    assert set(rows["action_id"].to_list()) == {"act_a1", "act_a2", "act_a4"}
    lines = csv_text.strip().split("\n")
    assert (
        lines[0]
        == "ACTION_ID,APPROVAL_ID,CUSTOMER,ITEM,UOM,EFFECTIVE_DATE,PREVIOUS_PRICE,NEW_PRICE"
    )
    assert len(lines) == 4
    assert "act_a3" not in csv_text and "act_a5" not in csv_text
    # Every row carries an approval id, dates and both prices.
    first = lines[1].split(",")
    assert first[0] == "act_a1" and first[1].startswith("apr_")
    assert first[5] == "2026-02-25" and first[6] == "10.00" and first[7] == "12.50"


def test_export_transitions_logs_and_is_idempotent(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    out = tmp_path / "prices.csv"
    r1 = export_price_file(store, RULES, out, who="demo-analyst", at=AT)
    assert r1.n_transitioned == 3
    assert out.exists() and out.with_suffix(".csv.summary.json").exists()
    status = replay_status(store.events())
    for aid in ("act_a1", "act_a2", "act_a4"):
        assert status.filter(pl.col("action_id") == aid)["status"][0] == "exported"
    # service_policy stays approved (not a price action), proposed stays put.
    assert status.filter(pl.col("action_id") == "act_a5")["status"][0] == "approved"
    first_bytes = out.read_bytes()
    r2 = export_price_file(store, RULES, out, who="demo-analyst", at=AT)
    assert r2.n_transitioned == 0  # no repeated transitions
    assert out.read_bytes() == first_bytes  # identical file
    assert r2.csv_text == r1.csv_text


def test_template_variants(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    cfg = PriceExportConfig(
        delimiter=";",
        decimal_separator=",",
        date_format="%d.%m.%Y",
        price_unit="dollars",
        headers={
            "action_id": "AKTION",
            "approval_id": "FREIGABE",
            "customer_id": "KUNDE",
            "item_id": "ARTIKEL",
            "uom": "ME",
            "effective_date": "GUELTIG_AB",
            "previous_price": "PREIS_ALT",
            "new_price": "PREIS_NEU",
        },
    )
    csv_text, _rows = build_price_file(store.actions(), store.events(), cfg)
    lines = csv_text.strip().split("\n")
    assert lines[0] == "AKTION;FREIGABE;KUNDE;ARTIKEL;ME;GUELTIG_AB;PREIS_ALT;PREIS_NEU"
    assert ";25.02.2026;10,00;12,50" in lines[1]
    cents_cfg = PriceExportConfig(price_unit="cents")
    cents_text, _ = build_price_file(store.actions(), store.events(), cents_cfg)
    assert cents_text.strip().split("\n")[1].endswith(",1000,1250")
    # A comma decimal with a comma delimiter is refused at config time.
    with pytest.raises(ValueError, match="delimiter"):
        PriceExportConfig(decimal_separator=",", delimiter=",")


def test_summary_never_sums_across_bases(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    _, rows = build_price_file(store.actions(), store.events(), RULES.actions.price_export)
    s = diff_summary(rows)
    assert s["n_changes"] == 3
    by_basis = s["expected_impact_by_basis_cents"]
    assert set(by_basis) == {
        "gross_price_opportunity_fixed_volume",
        "expected_contribution_change",
    }
    assert "total" not in s  # levels are reported per basis, never one number
    largest = s["largest_changes"]
    assert largest and largest[0]["change_pct"] == 25.0


@pytest.mark.parametrize("variant", ["default", "comma_decimal"])
def test_round_trip_through_engine_ingest(tmp_path: Path, variant: str) -> None:
    """Re-import the exported price file through the engine's own ingest (a
    mapping over the price file, plus a quantity column the test adds) and
    confirm every price matches to the cent."""
    from margin_engine.ingest.loader import load_lines
    from margin_engine.ingest.mapping import load_mapping

    store = _seeded_store(tmp_path)
    if variant == "default":
        cfg = RULES.actions.price_export
        delimiter, dec, datefmt = ",", ".", "%Y-%m-%d"
    else:
        cfg = PriceExportConfig(delimiter=";", decimal_separator=",", date_format="%d.%m.%Y")
        delimiter, dec, datefmt = ";", ",", "%d.%m.%Y"
    csv_text, rows = build_price_file(store.actions(), store.events(), cfg)

    # The ERP template has no quantity column; ingest needs one, so the test
    # (playing the ERP) adds QTY=1 to every row before re-importing.
    lines = csv_text.strip().split("\n")
    lines[0] += f"{delimiter}QTY"
    lines[1:] = [ln + f"{delimiter}1" for ln in lines[1:]]
    data_dir = tmp_path / f"reimport_{variant}"
    data_dir.mkdir()
    (data_dir / "prices.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    h = cfg.headers
    mapping_yaml = {
        "source_system": f"erp_price_file_{variant}",
        "lines_file": "prices.csv",
        "decimal_separator": dec,
        "input": {"delimiter": delimiter},
        "cost_unavailable": True,
        "lines": {
            "source_line_key": {"column": h["action_id"]},
            "invoice_id": {"column": h["approval_id"]},
            "invoice_date": {"column": h["effective_date"], "format": datefmt},
            "customer_id": {"column": h["customer_id"]},
            "item_id": {"column": h["item_id"]},
            "quantity": {"column": "QTY"},
            "uom": {"column": h["uom"]},
            "unit_price": {
                "column": h["new_price"],
                "currency": "reporting",
                "authoritative": True,
            },
            "transaction_type": {
                "derive_from_column": h["action_id"],
                "prefix_map": {},
                "default": "sale",
            },
        },
    }
    mpath = tmp_path / f"mapping_{variant}.yaml"
    mpath.write_text(yaml.safe_dump(mapping_yaml), encoding="utf-8")
    canonical = load_lines(data_dir, load_mapping(mpath))
    assert canonical.height == rows.height
    merged = canonical.select(
        pl.col("source_line_key").alias("action_id"),
        ((pl.col("unit_price_scaled") + 50) // 100).alias("reimported_cents"),
    ).join(rows.select("action_id", "proposed_price_cents"), on="action_id", how="inner")
    assert merged.height == rows.height
    assert (merged["reimported_cents"] == merged["proposed_price_cents"]).all()
    # Dates survived the template's format too.
    assert set(canonical["invoice_date"].to_list()) == {dt.date(2026, 2, 25)}
