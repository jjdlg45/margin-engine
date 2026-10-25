"""ERP-ready price export (Phase 40).

Approved price actions (price_change, below_floor_fix — the types that carry a
proposed price) become a price file an ERP can import: generic CSV by default,
with header names, date format, decimal separator and price unit configurable
per deployment (actions.price_export). Every row carries the effective date
(the action's due date), previous price, new price, UOM, approval id and
action id.

Idempotency: the file always contains every approved-or-already-exported price
action, formatted deterministically, so re-exporting rewrites the identical
file; only actions still in 'approved' receive the exported transition, so no
event is ever logged twice. A diff summary (count, expected impact BY BASIS —
opportunity levels are never summed across bases — and the largest changes) is
returned for display before the export and written beside the file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from margin_engine.actions.model import replay_status
from margin_engine.actions.store import ActionStore
from margin_engine.config import PriceExportConfig, Rules

PRICE_ACTION_TYPES = ("price_change", "below_floor_fix")
CANONICAL_COLUMNS = (
    "action_id",
    "approval_id",
    "customer_id",
    "item_id",
    "uom",
    "effective_date",
    "previous_price",
    "new_price",
)


@dataclass
class PriceExportResult:
    csv_text: str
    summary: dict[str, object]
    n_transitioned: int  # approved -> exported this call (0 on a re-export)


def _approval_ids(events: pl.DataFrame) -> pl.DataFrame:
    """Deterministic approval id per action: derived from the approving event
    (action, seq, approver, timestamp)."""
    approvals = events.filter(pl.col("to_status") == "approved").sort("action_id", "seq")
    if approvals.height == 0:
        return pl.DataFrame(schema={"action_id": pl.String, "approval_id": pl.String})
    rows = approvals.group_by("action_id", maintain_order=True).last()
    ids = [
        "apr_"
        + hashlib.sha256(
            f"{r['action_id']}|{r['seq']}|{r['who']}|{r['at'].isoformat()}".encode()
        ).hexdigest()[:10]
        for r in rows.iter_rows(named=True)
    ]
    return rows.select("action_id").with_columns(pl.Series("approval_id", ids))


def _fmt_price(cents: int, cfg: PriceExportConfig) -> str:
    if cfg.price_unit == "cents":
        return str(cents)
    sign = "-" if cents < 0 else ""
    mag = abs(cents)
    return f"{sign}{mag // 100}{cfg.decimal_separator}{mag % 100:02d}"


def build_price_file(
    actions: pl.DataFrame, events: pl.DataFrame, cfg: PriceExportConfig
) -> tuple[str, pl.DataFrame]:
    """(csv_text, exportable_rows). Only approved or already-exported price
    actions with a proposed price qualify; everything else never leaves the
    engine."""
    status = replay_status(events)
    rows = (
        actions.join(status, on="action_id", how="left", maintain_order="left")
        .with_columns(pl.col("status").fill_null("proposed"))
        .filter(
            pl.col("action_type").is_in(list(PRICE_ACTION_TYPES))
            & pl.col("status").is_in(["approved", "exported"])
            & pl.col("proposed_price_cents").is_not_null()
        )
        .join(_approval_ids(events), on="action_id", how="left", maintain_order="left")
        .sort("action_id")
    )
    header = [cfg.headers[c] for c in CANONICAL_COLUMNS]
    lines = [cfg.delimiter.join(header)]
    for r in rows.iter_rows(named=True):
        eff = r["due_date"]
        lines.append(
            cfg.delimiter.join(
                [
                    str(r["action_id"]),
                    str(r["approval_id"] or ""),
                    str(r["customer_id"] or ""),
                    str(r["item_id"] or ""),
                    str(r["uom"] or ""),
                    eff.strftime(cfg.date_format) if eff is not None else "",
                    _fmt_price(r["current_price_cents"], cfg)
                    if r["current_price_cents"] is not None
                    else "",
                    _fmt_price(int(r["proposed_price_cents"]), cfg),
                ]
            )
        )
    return "\n".join(lines) + "\n", rows


def diff_summary(rows: pl.DataFrame) -> dict[str, object]:
    """Count, expected impact BY BASIS (never summed across opportunity
    levels), and the largest changes — shown before export and written beside
    the file."""
    if rows.height == 0:
        return {
            "n_changes": 0,
            "expected_impact_by_basis_cents": {},
            "largest_changes": [],
        }
    by_basis = dict(
        rows.group_by("impact_basis")
        .agg(pl.col("expected_impact_cents").fill_null(0).sum())
        .sort("impact_basis")
        .iter_rows()
    )
    largest = (
        rows.filter(
            pl.col("current_price_cents").is_not_null() & (pl.col("current_price_cents") > 0)
        )
        .with_columns(
            (
                (pl.col("proposed_price_cents") - pl.col("current_price_cents"))
                / pl.col("current_price_cents")
                * 100
            )
            .round(2)
            .alias("change_pct")
        )
        .sort(["change_pct", "action_id"], descending=[True, False])
        .head(5)
        .select(
            "action_id",
            "customer_id",
            "item_id",
            "current_price_cents",
            "proposed_price_cents",
            "change_pct",
        )
        .to_dicts()
    )
    return {
        "n_changes": rows.height,
        "expected_impact_by_basis_cents": by_basis,
        "largest_changes": largest,
    }


def export_price_file(
    store: ActionStore,
    rules: Rules,
    out_path: Path | None,
    *,
    who: str,
    dry_run: bool = False,
    at: dt.datetime | None = None,
) -> PriceExportResult:
    """Build the price file and summary; unless dry_run, write both and move
    every still-approved row to 'exported' (logged). Re-running changes no
    state and rewrites the identical file."""
    cfg = rules.actions.price_export
    actions = store.actions()
    events = store.events()
    csv_text, rows = build_price_file(actions, events, cfg)
    summary = diff_summary(rows)
    n_transitioned = 0
    if not dry_run:
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(csv_text, encoding="utf-8", newline="\n")
            out_path.with_suffix(out_path.suffix + ".summary.json").write_text(
                json.dumps(summary, indent=1) + "\n", encoding="utf-8", newline="\n"
            )
        still_approved = rows.filter(pl.col("status") == "approved")["action_id"].to_list()
        for action_id in still_approved:
            store.transition(
                action_id,
                who=who,
                to_status="exported",
                note="included in the ERP price export",
                at=at,
            )
            n_transitioned += 1
    return PriceExportResult(csv_text=csv_text, summary=summary, n_transitioned=n_transitioned)
