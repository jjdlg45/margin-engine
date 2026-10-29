"""Pricing-law guardrails (Run 5, Phase 37; reworked in Run 6, Phase 79).

**Robinson-Patman review flag.** Two customers who may compete with each other
receiving prices for the same item that differ by more than the configured
threshold. It is a prompt to look, and the output never says more than that:
no engine output calls anything a violation, and none calls anything
compliant. Orientation, not legal advice - see docs/methodology.md.

Three things changed in Run 6 (Finding 17).

**Same industry and region is a candidate SCREEN, not a finding.** Two
distributors in the same industry and region may not compete at all - one
sells to shipyards, the other to hospitals - and two in different regions
may compete head to head. The screen is what the data can do on its own; a
reviewed customer-competitor map overrides it in both directions, confirming
pairs the screen missed and clearing pairs it caught. Every flagged pair says
which basis it rests on.

**A missing attribute is a data-quality status, not a quiet pass.** A
customer with no industry on file used to drop out of the comparison
silently. It is now counted, listed, and reported as unscreened - the
difference between "these customers do not compete" and "we do not know" is
the whole point of the flag.

**Price differences are the start of the question, not the answer.** The
defenses that matter - cost justification, meeting competition in good faith,
functional differences between buyers - all live in facts beside the price.
Each flagged pair carries the comparable item, unit of measure and item role;
the dates the prices and costs come from; both the current and the
recommended price on an invoice and a net basis; the volumes; the channel and
contract position; the discount, rebate and freight terms; the cost to serve
with the method that produced it; and what evidence a reviewer has recorded.
Cost differences are supporting evidence, never a conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from margin_engine.config import Rules

#: How a flagged pair came to be compared.
BASIS_REVIEWED = "reviewed competitor"
BASIS_SCREENED = "screened candidate"

#: Per-customer screen outcomes.
SCREEN_OK = "screened"
SCREEN_ATTRIBUTES_MISSING = "attributes missing"


@dataclass
class ComplianceResult:
    # One row per flagged pair x item, with the facts a reviewer needs.
    detail: pl.DataFrame
    # (customer_id, item_id, rp_review_flag) for every flagged guidance pair.
    flags: pl.DataFrame
    # Per-customer screen status, so an unscreened customer is visible.
    screen: pl.DataFrame
    notes: list[str] = field(default_factory=list)


_DETAIL_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "item_id": pl.String,
    "item_role": pl.String,
    "uom_basis": pl.String,
    "customer_id_a": pl.String,
    "customer_id_b": pl.String,
    "pair_basis": pl.String,
    "current_price_a_cents": pl.Int64,
    "current_price_b_cents": pl.Int64,
    "recommended_price_a_cents": pl.Int64,
    "recommended_price_b_cents": pl.Int64,
    "recommended_net_a_cents": pl.Int64,
    "recommended_net_b_cents": pl.Int64,
    "price_difference_pct": pl.Float64,
    "net_difference_pct": pl.Float64,
    "quantity_a": pl.Float64,
    "quantity_b": pl.Float64,
    "channel_a": pl.String,
    "channel_b": pl.String,
    "contract_a": pl.String,
    "contract_b": pl.String,
    "deduction_bp_a": pl.Int64,
    "deduction_bp_b": pl.Int64,
    "rebate_bp_a": pl.Int64,
    "rebate_bp_b": pl.Int64,
    "concession_bp_a": pl.Int64,
    "concession_bp_b": pl.Int64,
    "cts_per_unit_a_cents": pl.Int64,
    "cts_per_unit_b_cents": pl.Int64,
    "cts_difference_cents": pl.Int64,
    "cts_method": pl.String,
    "price_as_of": pl.String,
    "cost_as_of": pl.String,
    "data_coverage": pl.String,
    "meeting_competition_record": pl.String,
    "contemporaneous_quote": pl.String,
    "reviewer_decision": pl.String,
    "reviewer": pl.String,
    "reviewed_date": pl.String,
}

_SCREEN_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "customer_id": pl.String,
    "screen_status": pl.String,
    "missing_attributes": pl.String,
}

#: Fields a reviewer looks for; reported as present or absent per pair rather
#: than quietly omitted when the source has no column for them.
_COVERAGE_FIELDS = (
    ("quantity", ("quantity_a", "quantity_b")),
    ("channel", ("channel_a", "channel_b")),
    ("contract", ("contract_a", "contract_b")),
    ("terms", ("deduction_bp_a", "deduction_bp_b")),
    ("cost to serve", ("cts_per_unit_a_cents", "cts_per_unit_b_cents")),
)


def _empty(notes: list[str], screen: pl.DataFrame | None = None) -> ComplianceResult:
    return ComplianceResult(
        detail=pl.DataFrame(schema=_DETAIL_SCHEMA),
        flags=pl.DataFrame(
            schema={"customer_id": pl.String, "item_id": pl.String, "rp_review_flag": pl.Boolean}
        ),
        screen=screen if screen is not None else pl.DataFrame(schema=_SCREEN_SCHEMA),
        notes=notes,
    )


def screen_customers(customers: pl.DataFrame, attrs: list[str]) -> pl.DataFrame:
    """Which customers can be screened at all, and what is missing.

    A null attribute is not a value. Two customers with no industry on file
    are not thereby in the same industry, and polars will not join them -
    but silence is the wrong answer either way. They come back as
    `attributes missing` with the attributes named.
    """
    missing_expr = pl.concat_str(
        [
            pl.when(pl.col(a).is_null() | (pl.col(a).cast(pl.String).str.strip_chars() == ""))
            .then(pl.lit(a))
            .otherwise(pl.lit(""))
            for a in attrs
        ],
        separator=" ",
    ).str.strip_chars()
    return (
        customers.select("customer_id", *attrs)
        .with_columns(missing_expr.alias("missing_attributes"))
        .with_columns(
            pl.when(pl.col("missing_attributes") == "")
            .then(pl.lit(SCREEN_OK))
            .otherwise(pl.lit(SCREEN_ATTRIBUTES_MISSING))
            .alias("screen_status")
        )
        .select("customer_id", "screen_status", "missing_attributes")
        .sort("customer_id")
    )


def _normalize_map(competitor_map: pl.DataFrame) -> pl.DataFrame:
    """Reviewed pairs, with the two ids in a stable order and both directions
    collapsed, so (A, B) and (B, A) are one judgement rather than two."""
    lo = pl.min_horizontal("customer_id_a", "customer_id_b")
    hi = pl.max_horizontal("customer_id_a", "customer_id_b")
    rel = (
        pl.col("relationship").cast(pl.String).str.strip_chars().str.to_lowercase()
        if "relationship" in competitor_map.columns
        else pl.lit("competitor")
    )
    return (
        competitor_map.with_columns(
            lo.alias("customer_id_a"), hi.alias("customer_id_b"), rel.alias("relationship")
        )
        .unique(subset=["customer_id_a", "customer_id_b"], keep="first")
        .select("customer_id_a", "customer_id_b", "relationship")
    )


def _coverage(row_cols: set[str], frame: pl.DataFrame) -> pl.Expr:
    """A per-row list of which reviewer fields actually carry a value."""
    parts = []
    for name, cols in _COVERAGE_FIELDS:
        if not all(c in row_cols for c in cols):
            continue
        have = pl.col(cols[0]).is_not_null() & pl.col(cols[1]).is_not_null()
        parts.append(pl.when(have).then(pl.lit(name)).otherwise(pl.lit("")))
    if not parts:
        return pl.lit("none")
    del frame
    return (
        pl.concat_str(parts, separator=", ")
        .str.replace_all(r"(, )+", ", ")
        .str.strip_chars(", ")
        .replace("", "none")
    )


def run_robinson_patman(
    guidance: pl.DataFrame,
    customers: pl.DataFrame | None,
    cts_per_unit: pl.DataFrame | None,
    rules: Rules,
    competitor_map: pl.DataFrame | None = None,
    evidence: pl.DataFrame | None = None,
) -> ComplianceResult:
    cfg = rules.compliance.robinson_patman
    if not cfg.enabled:
        return _empty(["Robinson-Patman review flag OFF: disabled in config"])
    attrs = list(cfg.competitor_attributes)
    if customers is None:
        return _empty(
            [
                "Robinson-Patman review flag OFF: no customers reference table, "
                f"so the candidate screen ({' + '.join(attrs)}) cannot be evaluated"
            ]
        )
    missing = [a for a in attrs if a not in customers.columns]
    if missing:
        return _empty(
            [
                f"Robinson-Patman review flag OFF: customers table lacks "
                f"screen attribute(s) {missing}"
            ]
        )
    notes: list[str] = []
    screen = screen_customers(customers, attrs)
    unscreened = screen.filter(pl.col("screen_status") == SCREEN_ATTRIBUTES_MISSING)
    if unscreened.height:
        who = unscreened["customer_id"].to_list()
        shown = ", ".join(who[:5]) + (" and others" if len(who) > 5 else "")
        notes.append(
            f"Robinson-Patman screen: {unscreened.height} customer(s) have no value "
            f"for one or more screen attributes ({' + '.join(attrs)}) and could not "
            f"be screened ({shown}). Not screened is not the same as not competing; "
            "fill the attributes or add the pairs to the reviewed competitor map."
        )

    usable = guidance.filter(
        pl.col("target_index").is_not_null()
        & pl.col("eff_list_cents").is_not_null()
        & (pl.col("eff_list_cents") > 0)
    ).with_columns(
        (pl.col("target_index") * pl.col("eff_list_cents"))
        .round(0)
        .cast(pl.Int64)
        .alias("_rec_price_cents"),
    )
    # Every field below the price is optional: a minimal mapping still gets a
    # flag, and the data-coverage column says what the reviewer is missing
    # rather than the run failing or inventing it.
    cur = (
        (pl.col("current_index") * pl.col("eff_list_cents")).round(0).cast(pl.Int64)
        if "current_index" in usable.columns
        else pl.lit(None, dtype=pl.Int64)
    )
    usable = usable.with_columns(cur.alias("_cur_price_cents"))
    # The net basis: what the seller actually keeps after the deductions that
    # scale with price and the fixed ones that do not. A legal review that
    # compares invoice prices while one buyer takes a 4% rebate and the other
    # none is comparing the wrong two numbers.
    ded = pl.col("deduction_bp").fill_null(0) if "deduction_bp" in usable.columns else pl.lit(0)
    fixed = (
        pl.col("fixed_deduction_cents").fill_null(0)
        if "fixed_deduction_cents" in usable.columns
        else pl.lit(0)
    )
    usable = usable.with_columns(
        (pl.col("_rec_price_cents") * (10_000 - ded) / 10_000 - fixed)
        .round(0)
        .cast(pl.Int64)
        .alias("_rec_net_cents")
    )
    n_excluded = guidance.height - usable.height
    if n_excluded:
        notes.append(
            f"Robinson-Patman comparison: {n_excluded} guidance pair(s) have no "
            "usable list price and cannot be price-compared (excluded, not inferred)"
        )

    carry = [
        c
        for c in (
            "item_role",
            "uom_basis",
            "channel",
            "contract_id",
            "deduction_bp",
            "rebate_bp",
            "concession_bp",
            "total_qty",
            "last_sale_date",
            "cost_as_of",
        )
        if c in usable.columns
    ]
    base = usable.select(
        "customer_id",
        "item_id",
        "_rec_price_cents",
        "_cur_price_cents",
        "_rec_net_cents",
        *carry,
    ).join(
        screen.join(customers.select("customer_id", *attrs), on="customer_id", how="left"),
        on="customer_id",
        how="left",
    )
    # Only screened customers enter the screen join; the rest are reported
    # above rather than paired on a null.
    base_screened = base.filter(pl.col("screen_status") == SCREEN_OK)
    if cts_per_unit is not None:
        cts_cols = [c for c in cts_per_unit.columns if c not in ("customer_id", "item_id")]
        base_screened = base_screened.join(
            cts_per_unit.select("customer_id", "item_id", *cts_cols),
            on=["customer_id", "item_id"],
            how="left",
        )
    if "cts_per_unit_cents" not in base_screened.columns:
        base_screened = base_screened.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("cts_per_unit_cents")
        )

    renames = {
        "customer_id": "customer_id_{s}",
        "_rec_price_cents": "recommended_price_{s}_cents",
        "_cur_price_cents": "current_price_{s}_cents",
        "_rec_net_cents": "recommended_net_{s}_cents",
        "cts_per_unit_cents": "cts_per_unit_{s}_cents",
        "cts_method": "cts_method_{s}",
        "last_sale_date": "price_as_of_{s}",
        "cost_as_of": "cost_as_of_{s}",
        "channel": "channel_{s}",
        "contract_id": "contract_{s}",
        "deduction_bp": "deduction_bp_{s}",
        "rebate_bp": "rebate_bp_{s}",
        "concession_bp": "concession_bp_{s}",
        "total_qty": "quantity_{s}",
    }

    def side(suffix: str) -> pl.DataFrame:
        mapping = {k: v.format(s=suffix) for k, v in renames.items() if k in base_screened.columns}
        drop = [c for c in ("screen_status", "missing_attributes") if c in base_screened.columns]
        return base_screened.drop(drop).rename(mapping)

    keys = ["item_id", *attrs]
    left, right = side("a"), side("b")
    shared = [c for c in left.columns if c in right.columns and c not in keys]
    pairs = left.join(right.drop(shared), on=keys, how="inner").filter(
        pl.col("customer_id_a") < pl.col("customer_id_b")
    )

    # The reviewed map overrides the screen in BOTH directions.
    pairs = pairs.with_columns(pl.lit(BASIS_SCREENED).alias("pair_basis"))
    if competitor_map is not None and competitor_map.height:
        reviewed = _normalize_map(competitor_map)
        pairs = pairs.join(reviewed, on=["customer_id_a", "customer_id_b"], how="left")
        cleared = pairs.filter(pl.col("relationship") == "not_competitor").height
        pairs = pairs.filter(
            (pl.col("relationship") != "not_competitor") | pl.col("relationship").is_null()
        ).with_columns(
            pl.when(pl.col("relationship") == "competitor")
            .then(pl.lit(BASIS_REVIEWED))
            .otherwise(pl.lit(BASIS_SCREENED))
            .alias("pair_basis")
        )
        confirmed = reviewed.filter(pl.col("relationship") == "competitor")
        extra = confirmed.join(
            pairs.select("customer_id_a", "customer_id_b").unique(),
            on=["customer_id_a", "customer_id_b"],
            how="anti",
        )
        notes.append(
            f"Robinson-Patman screen: reviewed competitor map applied - "
            f"{confirmed.height} confirmed pair(s), {cleared} pair(s) cleared by a "
            f"reviewer, {extra.height} confirmed pair(s) the screen alone would not "
            "have found (they are compared only where both customers buy the item)"
        )
        pairs = pairs.drop("relationship")
        if cfg.require_reviewed_map:
            pairs = pairs.filter(pl.col("pair_basis") == BASIS_REVIEWED)
            notes.append(
                "Robinson-Patman screen: require_reviewed_map is on, so screened "
                "candidates that no reviewer has confirmed are not flagged"
            )
    elif cfg.require_reviewed_map:
        return _empty(
            [
                *notes,
                "Robinson-Patman review flag OFF: require_reviewed_map is on but no "
                "customer_competitors table was mapped, so no pair is confirmed",
            ],
            screen,
        )
    else:
        notes.append(
            "Robinson-Patman screen: no reviewed competitor map supplied, so every "
            "pair below is a screened candidate on "
            f"{' + '.join(attrs)} alone - a prompt to check whether the two customers "
            "actually compete, not a finding that they do"
        )

    lo = pl.min_horizontal("recommended_price_a_cents", "recommended_price_b_cents")
    hi = pl.max_horizontal("recommended_price_a_cents", "recommended_price_b_cents")
    net_lo = pl.min_horizontal("recommended_net_a_cents", "recommended_net_b_cents")
    net_hi = pl.max_horizontal("recommended_net_a_cents", "recommended_net_b_cents")
    pairs = pairs.with_columns(
        pl.when(lo > 0).then((hi - lo) / lo * 100).otherwise(None).alias("price_difference_pct"),
        pl.when(net_lo > 0)
        .then((net_hi - net_lo) / net_lo * 100)
        .otherwise(None)
        .alias("net_difference_pct"),
    )
    # Strictly greater than the threshold fires; the boundary itself does not.
    flagged = pairs.filter(
        pl.col("price_difference_pct").is_not_null()
        & (pl.col("price_difference_pct") > cfg.price_difference_threshold_pct)
    ).with_columns(
        (pl.col("cts_per_unit_a_cents") - pl.col("cts_per_unit_b_cents")).alias(
            "cts_difference_cents"
        ),
        _coverage(set(pairs.columns), pairs).alias("data_coverage"),
    )
    # Method and dates can differ between the two sides, and a reviewer needs
    # to know when they do rather than being shown one of them.
    for name in ("cts_method", "price_as_of", "cost_as_of"):
        a, b = f"{name}_a", f"{name}_b"
        if a in flagged.columns and b in flagged.columns:
            sa, sb = pl.col(a).cast(pl.String), pl.col(b).cast(pl.String)
            flagged = flagged.with_columns(
                pl.when(sa.is_null() & sb.is_null())
                .then(pl.lit(None, dtype=pl.String))
                .when(sa == sb)
                .then(sa)
                .otherwise(
                    pl.concat_str(
                        [
                            pl.lit("A: "),
                            sa.fill_null("not stated"),
                            pl.lit(", B: "),
                            sb.fill_null("not stated"),
                        ]
                    )
                )
                .alias(name)
            )
    flagged = _attach_evidence(flagged, evidence, notes)

    have = set(flagged.columns)
    detail = flagged.select(
        *[
            (pl.col(name) if name in have else pl.lit(None, dtype=dtype)).alias(name)
            for name, dtype in _DETAIL_SCHEMA.items()
        ]
    ).sort("item_id", "customer_id_a", "customer_id_b")
    flags = (
        pl.concat(
            [
                flagged.select(pl.col("customer_id_a").alias("customer_id"), "item_id"),
                flagged.select(pl.col("customer_id_b").alias("customer_id"), "item_id"),
            ]
        )
        .unique()
        .sort("customer_id", "item_id")
        .with_columns(pl.lit(value=True).alias("rp_review_flag"))
    )
    if detail.height:
        n_reviewed = detail.filter(pl.col("pair_basis") == BASIS_REVIEWED).height
        notes.append(
            f"Robinson-Patman review: {detail.height} pair-item combination(s) with "
            f"recommended prices more than {cfg.price_difference_threshold_pct:g}% "
            f"apart ({n_reviewed} reviewed competitor, "
            f"{detail.height - n_reviewed} screened candidate). Cost-to-serve "
            "differences and buyer terms are attached as supporting documentation. "
            "A prompt to review, not legal advice."
        )
    return ComplianceResult(detail=detail, flags=flags, screen=screen, notes=notes)


def _attach_evidence(
    flagged: pl.DataFrame, evidence: pl.DataFrame | None, notes: list[str]
) -> pl.DataFrame:
    """Join what a reviewer has already recorded about each pair.

    The defenses a reviewer actually needs - a meeting-competition record, a
    contemporaneous bid, a documented decision - are facts the engine cannot
    derive. It can only show whether they exist, and say so plainly when they
    do not.
    """
    cols = (
        "meeting_competition_record",
        "contemporaneous_quote",
        "reviewer_decision",
        "reviewer",
        "reviewed_date",
    )
    if evidence is None or not evidence.height:
        notes.append(
            "Robinson-Patman review: no evidence table mapped, so no pair carries a "
            "meeting-competition record, a contemporaneous bid or a reviewer's "
            "decision. Absent, not clear."
        )
        return flagged.with_columns(
            [pl.lit(None, dtype=pl.String).alias(c) for c in cols if c not in flagged.columns]
        )
    ev = _normalize_map(
        evidence.with_columns(
            pl.lit("competitor").alias("relationship")
            if "relationship" not in evidence.columns
            else pl.col("relationship")
        )
    ).drop("relationship")
    keep = [c for c in cols if c in evidence.columns]
    ev = ev.hstack(evidence.select(keep)) if keep else ev
    on = ["customer_id_a", "customer_id_b"]
    if "item_id" in evidence.columns and "item_id" in flagged.columns:
        ev = ev.hstack(evidence.select("item_id"))
        on = [*on, "item_id"]
    out = flagged.join(ev, on=on, how="left")
    for c in cols:
        if c not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=pl.String).alias(c))
    matched = out.filter(pl.col(keep[0]).is_not_null()).height if keep else 0
    notes.append(
        f"Robinson-Patman review: reviewer evidence attached to {matched} of "
        f"{out.height} flagged pair-item combination(s); the rest have none recorded"
    )
    return out
