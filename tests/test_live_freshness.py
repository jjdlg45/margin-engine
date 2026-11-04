"""Live mode: does every page actually move when the replay feed ticks?

Run 8. The replay feed re-runs the whole engine per data-day and republishes
the bundle, so in principle every page is rebuilt each tick. "In principle" is
how stale data gets shipped, so this drives the real scheduler through the
real API and reports, page by page, what changed and what did not.

A page that never changes is not automatically a bug - the segment tree can
legitimately land in the same place two days running - so this test asserts
the things that MUST move (the as-of date, and the figures that depend on
volume) and records the rest as findings, with `test_report_frozen_pages`
printing the inventory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from api.main import create_app
from api.replay import ReplayFeed
from api.settings import Settings
from api.store import PAGES
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
# The committed Online Retail II sample covers two business days, so a run
# gets two real ticks and a third that finds the feed exhausted.
EXPECTED_DAYS = ["2009-12-01", "2009-12-02"]


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


@pytest.fixture(scope="module")
def ticks(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    """Drive the replay feed through the API and snapshot every page per tick.

    Module-scoped because each tick is a full pipeline run.
    """
    tmp = tmp_path_factory.mktemp("live")
    common = {
        "bundle_dir": tmp / "no-bundle",
        "db_path": tmp / "api.duckdb",
        "actions_db": tmp / "actions.duckdb",
        "audit_log": tmp / "audit.log",
        "uploads_dir": tmp / "uploads",
        "drift_dir": tmp / "drift",
        "connectors_dir": tmp / "connectors",
        "connectors_spec": "",
        "replay_work_dir": tmp / "work",
        "replay_source_dir": REPO / "data" / "sample_online_retail",
        "fx_refresh_s": 100_000,
        "ppi_refresh_s": 100_000,
    }
    # The app runs with replay DISABLED and this test ticks the feed by hand
    # against the app's own store, so the sequence is deterministic. The trap,
    # found the hard way: Refresher._every runs its function immediately and
    # sleeps afterwards, so an enabled scheduler fires a tick the moment the
    # lifespan starts - however long the interval. That tick and a manual one
    # then race inside replay_day, where one call rmtree's the world directory
    # while the other is still copying reference files into it.
    app = create_app(Settings(**common, replay_enabled=False))
    feed_settings = Settings(**common, replay_enabled=True)

    snapshots: list[dict[str, Any]] = []
    with TestClient(app) as client:
        feed = ReplayFeed(feed_settings, app.state.store)
        assert feed.available_days() == EXPECTED_DAYS, (
            "the committed Online Retail II sample no longer covers the days this test replays"
        )
        for _ in range(len(EXPECTED_DAYS) + 1):
            day = feed.replay_next_day()
            app_payload = client.get("/api/app").json()
            pages = {}
            payloads = {}
            for page in PAGES:
                resp = client.get(f"/api/pages/{page}")
                ok = resp.status_code == 200
                pages[page] = _fingerprint(resp.json()) if ok else "ERROR"
                payloads[page] = resp.json() if ok else None
            snapshots.append(
                {
                    "replayed_day": day,
                    "data_as_of": app_payload.get("data_as_of"),
                    "data_label": app_payload.get("data_label"),
                    "kpis": app_payload.get("kpis"),
                    "refresh_status": app_payload.get("refresh_status"),
                    "pages": pages,
                    "payloads": payloads,
                }
            )
    return snapshots


def test_the_as_of_date_advances_with_each_replayed_day(ticks: list[dict[str, Any]]) -> None:
    """The headline freshness claim: "Data as of ..." has to be the replayed
    day, and has to move."""
    seen = [t["data_as_of"] for t in ticks]
    assert seen[: len(EXPECTED_DAYS)] == EXPECTED_DAYS, (
        f"as-of dates did not advance one business day per tick: {seen}"
    )
    # The tick after the last day finds the feed exhausted: it replays no day
    # and must NOT invent a newer as-of date.
    assert ticks[-1]["replayed_day"] is None, "the feed served a third day"
    assert seen[len(EXPECTED_DAYS)] == EXPECTED_DAYS[-1]
    # And the status the API serves says the data is current, not stale.
    status = ticks[-1]["refresh_status"]["replay"]
    assert status["stale"] is False, "an exhausted feed is complete, not stale"
    assert status["data_as_of"] == EXPECTED_DAYS[-1]


def test_the_data_label_names_the_day_and_calls_it_replayed(
    ticks: list[dict[str, Any]],
) -> None:
    """Freshness is only honest if the label says what the data is."""
    for tick, day in zip(ticks, EXPECTED_DAYS, strict=False):
        label = tick["data_label"]
        assert "Historical data replayed" in label, label
        assert day in label, f"label does not name its own as-of day: {label}"
        assert "not live" in label


def test_volume_dependent_figures_move_when_a_day_of_sales_arrives(
    ticks: list[dict[str, Any]],
) -> None:
    """A second day of invoices has to show up in the money."""
    first, second = ticks[0]["kpis"], ticks[1]["kpis"]
    assert second["net_revenue_cents"] > first["net_revenue_cents"], (
        "a whole extra business day of sales left revenue unchanged"
    )
    assert second["n_ledger_lines"] > first["n_ledger_lines"]
    assert second["n_sales_observations"] > first["n_sales_observations"]


def test_every_page_is_served_on_every_tick(ticks: list[dict[str, Any]]) -> None:
    """Whatever else changes, no page may start erroring mid-replay."""
    for i, tick in enumerate(ticks):
        broken = sorted(p for p, fp in tick["pages"].items() if fp == "ERROR")
        assert not broken, f"tick {i + 1} failed to serve: {broken}"


def test_report_frozen_pages(ticks: list[dict[str, Any]]) -> None:
    """The inventory item 4 asks for: which pages moved between the two real
    ticks, and which stayed byte-identical.

    Printed rather than asserted page-by-page, because a page CAN legitimately
    be unchanged - the same segment rules, the same scenario grid - and an
    assertion would turn that into a false alarm. What is asserted is the
    weaker, true claim: a replayed day of sales must move SOMETHING.
    """
    before, after = ticks[0]["pages"], ticks[1]["pages"]
    moved = sorted(p for p in PAGES if before[p] != after[p])
    frozen = sorted(p for p in PAGES if before[p] == after[p])
    print(f"\nreplay tick 1 -> 2 ({EXPECTED_DAYS[0]} -> {EXPECTED_DAYS[1]})")
    print(f"  pages that changed ({len(moved)}): {', '.join(moved) or 'none'}")
    print(f"  pages unchanged   ({len(frozen)}): {', '.join(frozen) or 'none'}")
    assert moved, (
        "a full extra business day of invoices changed no page at all - the "
        "replay rebuilt the bundle but nothing downstream saw it"
    )
    # Every frozen page has to be frozen because it has nothing to show on
    # this data, not because it is serving yesterday's numbers. Online Retail
    # II has no cost, no quotes, no substitution groups and no receipts, so
    # the cost-scenario and optional-module pages are empty by construction,
    # and the connections page is static connector documentation that no
    # amount of sales data would move. Anything ELSE going frozen is a
    # staleness bug and fails here.
    expected_static = {"connections", "modules", "scenarios"}
    unexplained = sorted(set(frozen) - expected_static)
    assert not unexplained, (
        "these pages carry sales-derived figures and did not change when a "
        f"whole business day of invoices arrived: {unexplained}"
    )
    final = ticks[1]["payloads"]
    assert not final["scenarios"]["grids"], (
        "the scenarios page has grids yet never changed - that is stale data, not an empty page"
    )
    modules = final["modules"]
    for module in ("win_curves", "cross_price", "inventory"):
        assert not modules.get(module), (
            f"the {module} module has content yet never changed - stale, not absent"
        )
        reason_key = f"{module}_absent_reason"
        assert modules.get(reason_key), (
            f"{module} is absent from the bundle without saying why ({reason_key})"
        )
