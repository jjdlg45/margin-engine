"""Cross-price effects within substitution groups (Phase 17).

Estimation happens ONLY within declared substitution groups and only where
identified. For each group pair (A, B): panel rows of A restricted to customers
who buy both items; FE-2SLS of log q_A on [log p_A, log p_B] (pair and month
fixed effects, within-transformed), instrumented by both items' supplier-cost
and category-tariff shocks, clustered by month. Evidence statuses mirror
Phase 6; when the two items share supplier AND category the instruments cannot
separate the two prices and the group is labeled, never estimated.

Opportunity adjustment: when raising A's price at causal own-price support and
an estimated positive cross effect exists, a recapture share of lost volume
shifts to the substitute: recapture = clip(cross / |own|, 0, 0.9). The
adjustment is labeled and shown next to, never merged into, the unadjusted
number.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from margin_engine.config import Rules
from margin_engine.sensitivity.estimate import (
    STATUS_DESCRIPTIVE,
    STATUS_INSUFFICIENT,
    _cluster_codes,
    _cluster_meat,
)

STATUS_CROSS = "iv_estimated_cross_elasticity"
STATUS_CROSS_CAUSAL = "causal_cross_elasticity_supported"


@dataclass
class CrossEstimate:
    group_id: str
    item_a: str
    item_b: str
    n_obs: int = 0
    n_customers: int = 0
    cross_elasticity: float | None = None
    cross_se: float | None = None
    own_elasticity: float | None = None
    min_first_stage_f: float | None = None
    status: str = STATUS_INSUFFICIENT
    reasons: str = ""

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def _pair_panel(panel: pl.DataFrame, item_a: str, item_b: str) -> pl.DataFrame:
    a = panel.filter((pl.col("item_id") == item_a) & (pl.col("qty") > 0))
    b = panel.filter(pl.col("item_id") == item_b).select(
        "customer_id",
        "month_idx",
        pl.col("log_price").alias("log_price_b"),
        pl.col("z_cost").alias("z_cost_b"),
        pl.col("z_tariff").alias("z_tariff_b"),
    )
    both = a.join(b, on=["customer_id", "month_idx"], how="inner")
    return both.filter(
        pl.col("log_qty").is_not_null()
        & pl.col("log_price").is_not_null()
        & pl.col("log_price_b").is_not_null()
        & pl.col("z_cost").is_not_null()
        & pl.col("z_cost_b").is_not_null()
    )


def estimate_cross_effects(panel: pl.DataFrame, groups: pl.DataFrame, rules: Rules) -> pl.DataFrame:
    sc = rules.sensitivity
    out: list[dict[str, object]] = []
    for (gid,), members in sorted(groups.group_by(["group_id"]), key=lambda kv: kv[0]):
        ids = sorted(members["item_id"].to_list())
        if len(ids) != 2:
            continue
        for item_a, item_b in ((ids[0], ids[1]), (ids[1], ids[0])):
            est = CrossEstimate(group_id=str(gid), item_a=item_a, item_b=item_b)
            both = _pair_panel(panel, item_a, item_b)
            est.n_obs = both.height
            est.n_customers = both["customer_id"].n_unique()
            if both.height < sc.min_observations or est.n_customers < 5:
                est.reasons = f"{both.height} obs / {est.n_customers} customers with both items"
                out.append(est.to_dict())
                continue
            import pandas as pd

            pdf = pd.DataFrame(
                {
                    c: both[c].to_numpy()
                    for c in (
                        "log_qty",
                        "log_price",
                        "log_price_b",
                        "z_cost",
                        "z_tariff",
                        "z_cost_b",
                        "z_tariff_b",
                        "pair",
                        "month_idx",
                        "cluster_month",
                    )
                }
            )
            cols = [
                "log_qty",
                "log_price",
                "log_price_b",
                "z_cost",
                "z_tariff",
                "z_cost_b",
                "z_tariff_b",
            ]
            # Pair FE only: in a single item-pair panel every instrument varies
            # purely by month, so month fixed effects would absorb them
            # completely. Seasonality is controlled explicitly instead.
            dm = pdf[cols].astype(float)
            dm = dm - dm.groupby(pdf["pair"].to_numpy()).transform("mean")
            m = pdf["month_idx"].to_numpy().astype(float)
            season = np.column_stack(
                [np.sin(2 * np.pi * (m % 12) / 12), np.cos(2 * np.pi * (m % 12) / 12)]
            )
            season = season - season.mean(axis=0)
            proj = season @ np.linalg.pinv(season.T @ season) @ season.T
            resid = dm.to_numpy() - proj @ dm.to_numpy()
            y = resid[:, 0]
            x = resid[:, 1:3]
            z = resid[:, 3:7]
            # Shared supplier AND category make zB == zA: unidentifiable.
            if np.linalg.matrix_rank(z.T @ z) < 3:
                est.status = STATUS_DESCRIPTIVE
                est.reasons = (
                    "instruments do not separate the two prices (shared supplier "
                    "and category); cross effect labeled, not estimated"
                )
                out.append(est.to_dict())
                continue
            clusters = pdf["cluster_month"].to_numpy()
            codes, n_g = _cluster_codes(clusters)
            zz_inv = np.linalg.pinv(z.T @ z)
            x_hat = z @ (zz_inv @ (z.T @ x))
            xtx = x_hat.T @ x
            # Identification: relative conditioning, not raw determinant
            # (within-transformed log price variation is numerically small).
            sv = np.linalg.svd(xtx, compute_uv=False)
            if sv[-1] <= 0 or sv[-1] / sv[0] < 1e-6:
                est.status = STATUS_DESCRIPTIVE
                est.reasons = "first stage has no separating variation"
                out.append(est.to_dict())
                continue
            beta = np.linalg.solve(xtx, x_hat.T @ y)
            est.own_elasticity = float(beta[0])
            est.cross_elasticity = float(beta[1])
            u = y - x @ beta
            meat = _cluster_meat(x_hat, u, codes, n_g)
            bread = np.linalg.pinv(xtx)
            vcov = bread @ meat @ bread.T * n_g / max(n_g - 1, 1)
            est.cross_se = float(np.sqrt(max(vcov[1, 1], 0)))
            # First-stage strength per endogenous regressor (cluster-robust F).
            fs = []
            for j in range(2):
                bz = zz_inv @ (z.T @ x[:, j])
                resid = x[:, j] - z @ bz
                m = _cluster_meat(z, resid, codes, n_g)
                v = zz_inv @ m @ zz_inv * n_g / max(n_g - 1, 1)
                try:
                    f = float(bz @ np.linalg.pinv(v) @ bz / z.shape[1])
                except np.linalg.LinAlgError:
                    f = 0.0
                fs.append(f)
            est.min_first_stage_f = min(fs)
            reasons = []
            if est.min_first_stage_f < sc.min_first_stage_f:
                est.status = STATUS_DESCRIPTIVE
                est.reasons = f"weak first stage (min F {est.min_first_stage_f:.1f})"
                out.append(est.to_dict())
                continue
            ci_width = 2 * 1.96 * est.cross_se
            if ci_width > sc.max_ci_width:
                reasons.append(f"CI width {ci_width:.2f} exceeds maximum")
            rationale_ok = all(i.exclusion_rationale for i in sc.instruments) and bool(
                sc.instruments
            )
            if not rationale_ok:
                reasons.append("exclusion rationale missing in config")
            est.status = STATUS_CROSS_CAUSAL if not reasons else STATUS_CROSS
            est.reasons = "; ".join(reasons)
            out.append(est.to_dict())
    return pl.DataFrame(out)


def substitution_adjusted_change(
    expected_change_cents: int,
    own_elasticity: float,
    cross_elasticity: float | None,
    substitute_margin_cents_per_unit: int,
    lost_units: float,
) -> tuple[int, str]:
    """Labeled substitution adjustment for a price-increase scenario: part of
    the lost volume shifts to the substitute and keeps its margin."""
    if cross_elasticity is None or cross_elasticity <= 0 or own_elasticity >= 0:
        return expected_change_cents, "no_substitution_adjustment"
    recapture = min(cross_elasticity / abs(own_elasticity), 0.9)
    recaptured = round(lost_units * recapture * substitute_margin_cents_per_unit)
    return expected_change_cents + recaptured, f"substitution_recapture={recapture:.2f}"
