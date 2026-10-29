"""Customer hierarchy (Phase 10): ship-to -> bill-to -> parent account.

Analytics can run at a configurable level. The ledger always stays at ship-to
grain; this module swaps the analysis key on an analytics frame, keeping the
original ship-to id for audit. Missing hierarchy attributes fall back to the
ship-to id (a customer with no parent is its own parent), labeled in notes.
"""

from __future__ import annotations

import polars as pl

from margin_engine.config import Rules


def hierarchy_map(customers: pl.DataFrame | None) -> pl.DataFrame | None:
    """ship_to -> bill_to -> parent mapping from the customers reference."""
    if customers is None or "customer_id" not in (
        customers.columns if customers is not None else []
    ):
        return None
    cols = [pl.col("customer_id").alias("ship_to_id")]
    if "bill_to_id" in customers.columns:
        cols.append(
            pl.coalesce(pl.col("bill_to_id").replace("", None), pl.col("customer_id")).alias(
                "bill_to_id"
            )
        )
    else:
        cols.append(pl.col("customer_id").alias("bill_to_id"))
    if "parent_account_id" in customers.columns:
        cols.append(
            pl.coalesce(pl.col("parent_account_id").replace("", None), pl.col("customer_id")).alias(
                "parent_id"
            )
        )
    else:
        cols.append(pl.col("customer_id").alias("parent_id"))
    return customers.select(cols)


def apply_analysis_level(
    df: pl.DataFrame, customers: pl.DataFrame | None, rules: Rules
) -> tuple[pl.DataFrame, list[str]]:
    """Swap `customer_id` to the configured analysis level.

    The original ship-to id is preserved in `ship_to_customer_id`. At the
    default `ship_to` level the frame is returned unchanged.
    """
    level = rules.hierarchy.analysis_level
    if level == "ship_to":
        return df, []
    hmap = hierarchy_map(customers)
    if hmap is None:
        return df, [
            f"hierarchy analysis_level={level} requested but no customers reference "
            f"with hierarchy columns is available; staying at ship_to"
        ]
    key = "bill_to_id" if level == "bill_to" else "parent_id"
    out = (
        df.join(
            hmap.select("ship_to_id", key), left_on="customer_id", right_on="ship_to_id", how="left"
        )
        .with_columns(
            pl.col("customer_id").alias("ship_to_customer_id"),
            pl.coalesce(pl.col(key), pl.col("customer_id")).alias("customer_id"),
        )
        .drop(key)
    )
    return out, [f"analytics grouped at hierarchy level: {level} (ledger stays ship-to)"]


def inherit_parent_contracts(
    lines: pl.DataFrame,
    contracts: pl.DataFrame | None,
    customers: pl.DataFrame | None,
) -> tuple[pl.DataFrame, list[str]]:
    """Contract protection recorded at the parent reaches the children.

    A national agreement is signed once, at the parent account, while the
    invoice lines carry ship-to ids and often no contract number of their own.
    Until Run 6 (Phase 85) protection was the line's own `contract_id` and
    nothing else, so every child line under a parent-level contract was
    treated as freely priceable - eligible for price-increase actions on
    exactly the customers a contract forbids repricing.

    A line inherits when all of these hold:
    - it carries no contract_id of its own (a line's own contract always wins);
    - the contracts table names the line's customer, bill-to or parent;
    - the contract's item_family matches the line's (a contract with no
      family covers all);
    - the invoice date falls inside the contract's start/end (an open end
      covers everything after the start).

    The inherited id lands in `contract_id` - which is what guidance,
    opportunity eligibility, the action guardrails and the segmentation
    exclusion all read - and `contract_inherited` marks the lines so the
    provenance is visible rather than indistinguishable from a line-level
    contract.
    """
    notes: list[str] = []
    if contracts is None or not contracts.height or "contract_id" not in lines.columns:
        return lines, notes
    required = {"contract_id", "customer_id"}
    if not required <= set(contracts.columns):
        notes.append(
            "contracts table present but lacks contract_id/customer_id; "
            "parent-contract inheritance skipped"
        )
        return lines.with_columns(pl.lit(value=False).alias("contract_inherited")), notes

    hmap = hierarchy_map(customers)
    if hmap is None:
        hmap = lines.select(
            pl.col("customer_id").alias("ship_to_id"),
            pl.col("customer_id").alias("bill_to_id"),
            pl.col("customer_id").alias("parent_id"),
        ).unique()

    # One row per (account the contract names, contract): the contract can
    # name a ship-to, a bill-to or a parent, and each child resolves through
    # the hierarchy to all three.
    con = contracts.select(
        pl.col("customer_id").alias("_contract_account"),
        pl.col("contract_id").alias("_inherited_contract_id"),
        *(
            [pl.col("item_family").alias("_contract_family")]
            if "item_family" in contracts.columns
            else [pl.lit(None, dtype=pl.String).alias("_contract_family")]
        ),
        *(
            [pl.col("start_date").alias("_contract_start")]
            if "start_date" in contracts.columns
            else [pl.lit(None, dtype=pl.Date).alias("_contract_start")]
        ),
        *(
            [pl.col("end_date").alias("_contract_end")]
            if "end_date" in contracts.columns
            else [pl.lit(None, dtype=pl.Date).alias("_contract_end")]
        ),
    )

    work = lines.join(hmap, left_on="customer_id", right_on="ship_to_id", how="left")
    candidates = []
    for level in ("customer_id", "bill_to_id", "parent_id"):
        if level not in work.columns:
            continue
        candidates.append(
            work.select(
                "source_line_key",
                pl.col(level).alias("_acct"),
                *(
                    ["item_family"]
                    if "item_family" in work.columns
                    else [pl.lit(None, dtype=pl.String).alias("item_family")]
                ),
                "invoice_date",
            )
            .join(con, left_on="_acct", right_on="_contract_account", how="inner")
            .filter(
                (
                    pl.col("_contract_family").is_null()
                    | (pl.col("_contract_family") == pl.col("item_family"))
                )
                & (
                    pl.col("_contract_start").is_null()
                    | (pl.col("invoice_date") >= pl.col("_contract_start"))
                )
                & (
                    pl.col("_contract_end").is_null()
                    | (pl.col("invoice_date") <= pl.col("_contract_end"))
                )
            )
            .select("source_line_key", "_inherited_contract_id")
        )
    if not candidates:
        return lines.with_columns(pl.lit(value=False).alias("contract_inherited")), notes
    matched = (
        pl.concat(candidates)
        # Deterministic pick when several contracts cover one line.
        .sort("source_line_key", "_inherited_contract_id")
        .unique(subset=["source_line_key"], keep="first")
    )
    out = lines.join(matched, on="source_line_key", how="left", maintain_order="left")
    inherit = pl.col("contract_id").is_null() & pl.col("_inherited_contract_id").is_not_null()
    n = int(out.select(inherit.sum()).item())
    out = out.with_columns(
        pl.when(inherit)
        .then(pl.col("_inherited_contract_id"))
        .otherwise(pl.col("contract_id"))
        .alias("contract_id"),
        inherit.alias("contract_inherited"),
    ).drop("_inherited_contract_id")
    if n:
        notes.append(
            f"{n} line(s) inherited contract protection from a contract recorded "
            "at the account or parent level (contracts table); they are excluded "
            "from price-increase eligibility and pass-through like any other "
            "protected line, and contract_inherited marks them"
        )
    return out, notes
