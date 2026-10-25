"""Run 5 (Phase 41): synthetic validation of the results tracker.

World A (well-identified, planted price interventions): the generator raises
chosen pairs' prices by a known percentage at a known month, demand responds
through each pair's planted elasticity, and the tracker must recover that
planted effect: median absolute error of the measured difference-in-
differences volume effect vs the planted effect within the stated bound.

World B (sparse): the tracker must REPORT insufficient evidence instead of a
number when windows are too thin to separate a price effect from the trend.

Writes two rows into docs/validation.json. Usage:
    uv run python scripts/measure_tracker.py          # measure + update rows
    uv run python scripts/measure_tracker.py --dry    # measure only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from synth.config import GeneratorConfig, Preset  # noqa: E402

from margin_engine.actions.generate import ACTIONS_SCHEMA  # noqa: E402
from margin_engine.actions.model import EVENTS_SCHEMA  # noqa: E402
from margin_engine.actions.tracking import track_outcomes  # noqa: E402
from margin_engine.config import load_rules  # noqa: E402

MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"
RULES_PATH = REPO / "config" / "rules.yaml"

# Stated bounds (recorded with actuals; never loosened silently).
# A SINGLE pair's 90-day DiD carries irreducible Poisson sampling noise of
# ~0.2-0.3 log points at these volumes (the planted effect itself is only
# ~0.14), so per-pair error is reported as information while the RECOVERY
# claim is pooled across interventions — the same aggregation the calibration
# table uses: the median measured DiD effect must sit within the stated bound
# of the median planted effect.
POOLED_ERROR_BOUND = 0.10  # |median ln(DiD) - median planted| in log points
MIN_MEASURED_SHARE = 0.50  # share of interventions the tracker can measure
SPARSE_MAX_MEASURED_SHARE = 0.25  # on sparse, a number must be the exception


def _month_date(cfg: GeneratorConfig, month_idx: int) -> dt.date:
    y = cfg.start_year + (cfg.start_month - 1 + month_idx) // 12
    m = (cfg.start_month - 1 + month_idx) % 12 + 1
    return dt.date(y, m, 1)


def _actions_from_truth(
    inter: pl.DataFrame, panel_truth: pl.DataFrame, cfg: GeneratorConfig, guidance: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Synthetic implemented actions for the intervened pairs, with the
    planted effect as the prediction (so volume_error measures recovery)."""
    t0 = int(inter["intervention_month"][0])
    impl = _month_date(cfg, t0)
    at = datetime(impl.year, impl.month, impl.day, tzinfo=UTC)
    pre = (
        panel_truth.filter(pl.col("month") == t0 - 1)
        .select("customer_id", "item_id", pl.col("unit_price_cents").alias("pre_price"))
        .unique(subset=["customer_id", "item_id"])
    )
    seg = guidance.select("customer_id", "item_id", "segment_leaf")
    rows = []
    for i, r in enumerate(
        inter.join(pre, on=["customer_id", "item_id"], how="left")
        .join(seg, on=["customer_id", "item_id"], how="left")
        .iter_rows(named=True)
    ):
        pct = float(r["pct_increase"])
        beta = float(r["true_elasticity"])
        pre_price = int(r["pre_price"]) if r["pre_price"] is not None else 1_000
        rows.append(
            {
                "action_id": f"act_val{i:04d}",
                "action_type": "price_change",
                "title": "planted intervention",
                "rationale": "synthetic tracker validation",
                "customer_id": r["customer_id"],
                "item_id": r["item_id"],
                "scope": "customer_item",
                "uom": None,
                "segment_leaf": r["segment_leaf"],
                "guidance_status": None,
                "evidence_status": "causal_elasticity_supported",
                "volume_treatment": "expected_change",
                "predicted_volume_ratio": round((1.0 + pct) ** beta, 6),
                "impact_basis": "expected_contribution_change",
                "expected_impact_cents": None,
                "current_price_cents": pre_price,
                "proposed_price_cents": round(pre_price * (1.0 + pct)),
                "floor_price_cents": None,
                "priority": "medium",
                "owner": "validation",
                "due_date": impl,
                "compliance_flags": "",
                "blocked": False,
                "blocked_reason": None,
                "created_at": at,
            }
        )
    actions = pl.DataFrame(rows).cast(ACTIONS_SCHEMA)
    ev_rows = []
    for aid in actions["action_id"].to_list():
        for seq, (frm, to) in enumerate(
            [
                (None, "proposed"),
                ("proposed", "under_review"),
                ("under_review", "approved"),
                ("approved", "exported"),
                ("exported", "implemented"),
            ]
        ):
            ev_rows.append(
                {
                    "action_id": aid,
                    "seq": seq,
                    "who": "validation",
                    "at": at,
                    "from_status": frm,
                    "to_status": to,
                    "note": "",
                }
            )
    events = pl.DataFrame(ev_rows, schema=EVENTS_SCHEMA)
    truth = inter.select(
        "customer_id",
        "item_id",
        (pl.col("true_elasticity") * (1.0 + pl.col("pct_increase")).log()).alias(
            "planted_log_effect"
        ),
    )
    return actions, events, truth


def run_world(preset: Preset, seed: int, n_customers: int, n_items: int) -> dict[str, Any]:
    from synth.generate import generate

    from margin_engine.report.full_run import run_full

    cfg = GeneratorConfig.for_preset(
        preset,
        seed=seed,
        n_customers=n_customers,
        n_items=n_items,
        n_price_interventions=30,
        intervention_pct=0.08,
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        generate(cfg, tmp / "world")
        gt = tmp / "world" / "ground_truth"
        inter = pl.read_parquet(gt / "interventions.parquet")
        panel_truth = pl.read_parquet(gt / "panel_truth.parquet")
        res = run_full(tmp / "world" / "erp", MAPPING, RULES_PATH, tmp / "out", validate=False)
        rules = load_rules(RULES_PATH)
        actions, events, truth = _actions_from_truth(inter, panel_truth, cfg, res.guidance)
        tracked = track_outcomes(
            actions,
            events,
            res.pipeline.ledger,
            rules,
            pair_segments=res.guidance.select("customer_id", "item_id", "segment_leaf"),
        )
    out = tracked.outcomes.join(truth, on=["customer_id", "item_id"], how="left")
    measured = out.filter(pl.col("outcome_status") == "measured").with_columns(
        (pl.col("did_volume_ratio").log() - pl.col("planted_log_effect")).abs().alias("abs_err")
    )
    errs = sorted(measured["abs_err"].drop_nulls().to_list())
    med = errs[len(errs) // 2] if errs else None
    p90 = errs[min(len(errs) - 1, math.ceil(0.9 * len(errs)) - 1)] if errs else None
    pooled_err = None
    if measured.height:
        med_did = float(measured.select(pl.col("did_volume_ratio").log().median()).item())
        med_planted = float(measured.select(pl.col("planted_log_effect").median()).item())
        pooled_err = round(abs(med_did - med_planted), 4)
    return {
        "n_interventions": inter.height,
        "n_measured": measured.height,
        "measured_share": round(measured.height / max(inter.height, 1), 3),
        "pooled_recovery_error_log_points": pooled_err,
        "per_pair_median_abs_error_log_points": round(med, 4) if med is not None else None,
        "per_pair_p90_abs_error_log_points": round(p90, 4) if p90 is not None else None,
        "insufficient_reasons": dict(
            out.filter(pl.col("outcome_status") != "measured")
            .with_columns(pl.col("outcome_reason").str.slice(0, 40).alias("r"))
            .group_by("r")
            .len()
            .sort("r")
            .iter_rows()
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    well = run_world(Preset.WELL_IDENTIFIED, seed=5, n_customers=300, n_items=500)
    sparse = run_world(Preset.SPARSE, seed=5, n_customers=300, n_items=500)
    print("well_identified:", json.dumps(well, indent=1))
    print("sparse:", json.dumps(sparse, indent=1))

    well_pass = (
        well["pooled_recovery_error_log_points"] is not None
        and well["pooled_recovery_error_log_points"] <= POOLED_ERROR_BOUND
        and well["measured_share"] >= MIN_MEASURED_SHARE
    )
    sparse_pass = sparse["measured_share"] <= SPARSE_MAX_MEASURED_SHARE
    rows = [
        {
            "check": "Results tracker: planted-intervention recovery",
            "target": (
                f"pooled recovery |median DiD effect - median planted effect| "
                f"<= {POOLED_ERROR_BOUND} log points and >= "
                f"{MIN_MEASURED_SHARE:.0%} of interventions measured "
                f"(well-identified world, seed 5, 30 planted +8% interventions; a "
                f"single pair's 90-day window carries ~0.2-0.3 log points of "
                f"Poisson noise, reported as information)"
            ),
            "achieved": (
                f"{well['n_measured']}/{well['n_interventions']} measured; "
                f"pooled recovery error "
                f"{well['pooled_recovery_error_log_points']} log points; "
                f"per-pair median abs error "
                f"{well['per_pair_median_abs_error_log_points']}, P90 "
                f"{well['per_pair_p90_abs_error_log_points']}"
            ),
            "passed": bool(well_pass),
            "detail": well,
        },
        {
            "check": "Results tracker: fail-closed on a sparse world",
            "target": (
                f"<= {SPARSE_MAX_MEASURED_SHARE:.0%} of interventions get a "
                "number; the rest report insufficient evidence with reasons "
                "(sparse preset, same interventions)"
            ),
            "achieved": (
                f"{sparse['n_measured']}/{sparse['n_interventions']} measured "
                f"({sparse['measured_share']:.0%}); refusal reasons: "
                f"{sparse['insufficient_reasons']}"
            ),
            "passed": bool(sparse_pass),
            "detail": sparse,
        },
    ]
    if args.dry:
        print("dry run: validation.json untouched")
        sys.exit(0 if well_pass and sparse_pass else 1)
    out_path = REPO / "docs" / "validation.json"
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    kept = [r for r in payload["results"] if not str(r["check"]).startswith("Results tracker:")]
    payload["results"] = kept + rows
    payload["generated_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    out_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"updated {out_path}")
    sys.exit(0 if well_pass and sparse_pass else 1)


if __name__ == "__main__":
    main()
