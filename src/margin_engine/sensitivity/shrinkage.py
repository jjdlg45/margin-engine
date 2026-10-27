"""Empirical-Bayes partial pooling of sub-segment elasticities toward the
parent commercial segment (Phase 6, spec 8.6).

Children are segment x item_category cells. Each child's raw IV estimate is
shrunk toward the parent segment estimate with precision weights:

    shrunk = (b_child / se_child^2 + b_parent / tau^2) / (1/se_child^2 + 1/tau^2)

where tau^2 is the method-of-moments between-child variance (clipped at 0).
Raw estimate, shrunk estimate, effective sample size and uncertainty are all
reported; nothing is silently replaced.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from margin_engine.config import Rules
from margin_engine.sensitivity.estimate import SegmentEstimate, estimate_segment


def shrink_to_segment(
    panel: pl.DataFrame, parent_estimates: dict[int, SegmentEstimate], rules: Rules
) -> pl.DataFrame:
    """Child = segment x item_category. Returns one row per child with raw,
    shrunk, ESS and uncertainty columns."""
    rows: list[dict[str, object]] = []
    children = (
        panel.filter(pl.col("segment_leaf").is_not_null())
        .select("segment_leaf", "item_category")
        .unique()
        .sort("segment_leaf", "item_category")
    )
    # First pass: raw child estimates.
    raw: list[tuple[int, str, SegmentEstimate]] = []
    for seg, cat in children.iter_rows():
        child_panel = panel.filter(
            (pl.col("segment_leaf") == seg) & (pl.col("item_category") == cat)
        )
        # Partial pooling shrinks the CONDITIONAL estimate toward its parent;
        # the total-quantity estimand is not pooled, so fitting it per child
        # segment would be discarded work.
        est = estimate_segment(child_panel, rules, int(seg), with_total_quantity=False)
        raw.append((int(seg), str(cat), est))

    # tau^2 per segment via method of moments on child estimates.
    for seg in sorted({s for s, _, _ in raw}):
        parent = parent_estimates.get(seg)
        if parent is None or parent.iv is None:
            continue
        kids = [(c, e) for s, c, e in raw if s == seg and e.iv is not None and e.iv_se]
        if kids:
            b = np.array([e.iv for _, e in kids])
            v = np.array([(e.iv_se or 0.0) ** 2 for _, e in kids])
            between = float(max(np.var(b) - np.mean(v), 0.0)) if len(kids) > 1 else 0.0
        else:
            between = 0.0
        for cat, e in kids:
            assert e.iv_se is not None
            se2 = e.iv_se**2
            if between > 0:
                w_child = 1 / se2
                w_parent = 1 / between
            else:
                w_child, w_parent = 0.0, 1.0  # no between-variance: full pooling
            shrunk = (e.iv * w_child + parent.iv * w_parent) / (w_child + w_parent)  # type: ignore[operator]
            shrunk_se = float(np.sqrt(1 / (w_child + w_parent))) if w_child else None
            rows.append(
                {
                    "segment": seg,
                    "item_category": cat,
                    "raw_elasticity": e.iv,
                    "raw_se": e.iv_se,
                    "parent_elasticity": parent.iv,
                    "shrunk_elasticity": float(shrunk),
                    "shrunk_se": shrunk_se,
                    "effective_sample_size": e.n_positive,
                    "shrink_weight_parent": float(w_parent / (w_child + w_parent)),
                    "status": e.status,
                }
            )
        # Children with no estimable elasticity inherit the parent, fully labeled.
        for s2, cat, e in raw:
            if s2 == seg and (e.iv is None or not e.iv_se):
                rows.append(
                    {
                        "segment": seg,
                        "item_category": cat,
                        "raw_elasticity": None,
                        "raw_se": None,
                        "parent_elasticity": parent.iv,
                        "shrunk_elasticity": parent.iv,
                        "shrunk_se": None,
                        "effective_sample_size": e.n_positive,
                        "shrink_weight_parent": 1.0,
                        "status": "insufficient_data",
                    }
                )
    return pl.DataFrame(rows)
