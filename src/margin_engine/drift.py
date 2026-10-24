"""Drift monitoring for live mode (Run 5, Phase 43).

Each refresh compares against the previous run: data-quality rates, segment
assignments (adjusted Rand index over the lines both runs share), elasticity
estimates and statuses, and guidance status counts. Changes beyond the
configured thresholds become flags with plain-English explanations ("Segment
2's price sensitivity moved from -1.40 to -0.90 since the previous refresh"),
served by the API and shown in the UI. Small moves stay quiet; the thresholds
are config, never code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

from margin_engine.config import Rules
from margin_engine.labels import label as label_of

if TYPE_CHECKING:
    from margin_engine.report.full_run import FullRunResult


@dataclass
class DriftResult:
    flags: list[dict[str, str]] = field(default_factory=list)
    # The raw comparison numbers behind every flag (and non-flag), for the API.
    detail: dict[str, Any] = field(default_factory=dict)


def snapshot_from_result(res: FullRunResult) -> dict[str, Any]:
    """A compact, serializable picture of one run — everything the next
    refresh needs to detect drift. Assignments ride as a separate frame."""
    ledger = res.pipeline.ledger
    sale = ledger.filter(pl.col("transaction_type") == "sale")
    net = int(sale["net_amount_cents"].sum() or 0)
    missing_cost = int(sale.filter(pl.col("flag_missing_cost"))["net_amount_cents"].sum() or 0)
    flagged = res.flagged
    sens = res.sensitivity.select("segment", "iv", "status").sort("segment")
    return {
        "data_quality": {
            "missing_cost_revenue_share": missing_cost / net if net else 0.0,
            "data_quality_anomaly_share": (
                float(flagged["flag_data_quality_anomaly"].mean() or 0.0)  # type: ignore[arg-type]
                if flagged.height
                else 0.0
            ),
            "commercial_outlier_share": (
                float(flagged["flag_commercial_pricing_outlier"].mean() or 0.0)  # type: ignore[arg-type]
                if flagged.height
                else 0.0
            ),
            "reconciliation_pass": bool(res.pipeline.reconciliation.all_pass),
        },
        "sensitivity": sens.to_dicts(),
        "guidance_status_counts": dict(
            res.guidance.group_by("guidance_status").len().sort("guidance_status").iter_rows()
        ),
        "n_ledger_lines": ledger.height,
    }


def _fmt_share(x: float) -> str:
    return f"{x * 100:.1f}%"


def compare_runs(
    prev: dict[str, Any],
    curr: dict[str, Any],
    prev_assignments: pl.DataFrame | None,
    curr_assignments: pl.DataFrame | None,
    rules: Rules,
) -> DriftResult:
    dc = rules.drift
    out = DriftResult()
    if not dc.enabled:
        return out

    # --- data-quality rates --------------------------------------------------
    for key, name in (
        ("missing_cost_revenue_share", "revenue with no known cost"),
        ("data_quality_anomaly_share", "lines flagged as data-entry anomalies"),
        ("commercial_outlier_share", "lines flagged as commercial pricing outliers"),
    ):
        a = float(prev["data_quality"].get(key, 0.0))
        b = float(curr["data_quality"].get(key, 0.0))
        delta_pp = (b - a) * 100
        out.detail.setdefault("data_quality", {})[key] = {
            "previous": a,
            "current": b,
            "change_pp": round(delta_pp, 3),
        }
        if round(abs(delta_pp), 6) > dc.data_quality_change_pp:
            direction = "rose" if delta_pp > 0 else "fell"
            out.flags.append(
                {
                    "code": f"data_quality:{key}",
                    "message": (
                        f"The share of {name} {direction} from {_fmt_share(a)} to "
                        f"{_fmt_share(b)} since the previous refresh."
                    ),
                }
            )
    if prev["data_quality"].get("reconciliation_pass") and not curr["data_quality"].get(
        "reconciliation_pass"
    ):
        out.flags.append(
            {
                "code": "data_quality:reconciliation",
                "message": "Reconciliation passed on the previous refresh and fails now.",
            }
        )

    # --- segment assignments (ARI over shared lines) --------------------------
    if prev_assignments is not None and curr_assignments is not None:
        joined = prev_assignments.rename({"segment_leaf": "_prev"}).join(
            curr_assignments.rename({"segment_leaf": "_curr"}),
            on="source_line_key",
            how="inner",
        )
        if joined.height >= dc.min_common_lines:
            from sklearn.metrics import adjusted_rand_score

            ari = float(adjusted_rand_score(joined["_prev"].to_numpy(), joined["_curr"].to_numpy()))
            out.detail["segment_ari"] = {"ari": round(ari, 4), "n_common_lines": joined.height}
            if ari < dc.ari_warn_below:
                out.flags.append(
                    {
                        "code": "segments:ari",
                        "message": (
                            f"Segment assignments moved: agreement with the previous "
                            f"refresh is {ari:.2f} (adjusted Rand index over "
                            f"{joined.height:,} shared lines), below the "
                            f"{dc.ari_warn_below:.2f} threshold. Customers may land in "
                            f"different reference bands than last time."
                        ),
                    }
                )
        else:
            out.detail["segment_ari"] = {
                "ari": None,
                "n_common_lines": joined.height,
                "note": "too few shared lines to compare",
            }

    # --- elasticity estimates and statuses -------------------------------------
    prev_sens = {int(r["segment"]): r for r in prev.get("sensitivity", [])}
    sens_detail: list[dict[str, Any]] = []
    for r in curr.get("sensitivity", []):
        seg = int(r["segment"])
        p = prev_sens.get(seg)
        if p is None:
            continue
        a_iv, b_iv = p.get("iv"), r.get("iv")
        row: dict[str, Any] = {"segment": seg, "previous_iv": a_iv, "current_iv": b_iv}
        if (
            a_iv is not None
            and b_iv is not None
            and abs(float(b_iv) - float(a_iv)) > dc.elasticity_abs_change
        ):
            out.flags.append(
                {
                    "code": f"sensitivity:elasticity:{seg}",
                    "message": (
                        f"Segment {seg}'s price sensitivity moved from "
                        f"{float(a_iv):.2f} to {float(b_iv):.2f} since the previous "
                        f"refresh."
                    ),
                }
            )
        if p.get("status") != r.get("status"):
            row["status_change"] = f"{p.get('status')} -> {r.get('status')}"
            out.flags.append(
                {
                    "code": f"sensitivity:status:{seg}",
                    "message": (
                        f"Segment {seg}'s evidence status changed from "
                        f'"{label_of(str(p.get("status")))}" to '
                        f'"{label_of(str(r.get("status")))}" since the previous '
                        f"refresh."
                    ),
                }
            )
        sens_detail.append(row)
    out.detail["sensitivity"] = sens_detail

    # --- guidance status counts -------------------------------------------------
    pc = {str(k): int(v) for k, v in prev.get("guidance_status_counts", {}).items()}
    cc = {str(k): int(v) for k, v in curr.get("guidance_status_counts", {}).items()}
    status_detail: dict[str, Any] = {}
    for status in sorted(set(pc) | set(cc)):
        a_n, b_n = pc.get(status, 0), cc.get(status, 0)
        status_detail[status] = {"previous": a_n, "current": b_n}
        base = max(a_n, 1)
        if (
            a_n != b_n
            and abs(b_n - a_n) / base > dc.status_share_change
            and abs(b_n - a_n) >= dc.min_status_count_change
        ):
            direction = "rose" if b_n > a_n else "fell"
            out.flags.append(
                {
                    "code": f"guidance:status:{status}",
                    "message": (
                        f'The number of pairs with status "{label_of(status)}" '
                        f"{direction} from {a_n:,} to {b_n:,} since the previous "
                        f"refresh."
                    ),
                }
            )
    out.detail["guidance_status_counts"] = status_detail
    return out


class DriftStore:
    """Previous-run snapshot on disk (JSON + assignments parquet), plus the
    latest flags for the API to serve."""

    def __init__(self, dir_path: Path) -> None:
        self.dir = dir_path
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def _snap_path(self) -> Path:
        return self.dir / "previous_snapshot.json"

    @property
    def _assign_path(self) -> Path:
        return self.dir / "previous_assignments.parquet"

    @property
    def _flags_path(self) -> Path:
        return self.dir / "latest_flags.json"

    def previous(self) -> tuple[dict[str, Any] | None, pl.DataFrame | None]:
        snap = None
        if self._snap_path.exists():
            snap = json.loads(self._snap_path.read_text(encoding="utf-8"))
        assignments = pl.read_parquet(self._assign_path) if self._assign_path.exists() else None
        return snap, assignments

    def refresh(self, res: FullRunResult, rules: Rules) -> DriftResult:
        """Compare this run against the stored previous one, persist the flags
        and make this run the new baseline."""
        curr = snapshot_from_result(res)
        curr_assignments = res.segments.leaf_assignments
        prev, prev_assignments = self.previous()
        result = DriftResult()
        if prev is not None:
            result = compare_runs(prev, curr, prev_assignments, curr_assignments, rules)
        self._snap_path.write_text(json.dumps(curr, indent=1), encoding="utf-8", newline="\n")
        curr_assignments.write_parquet(self._assign_path)
        self._flags_path.write_text(
            json.dumps({"flags": result.flags, "detail": result.detail}, indent=1),
            encoding="utf-8",
            newline="\n",
        )
        return result

    def latest(self) -> dict[str, Any]:
        if not self._flags_path.exists():
            return {"flags": [], "detail": {}}
        return json.loads(self._flags_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
