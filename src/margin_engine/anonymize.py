"""Anonymizer (Run 4, 27.5): `margin-engine anonymize` rewrites a source
export so results can be shared without exposing data.

- Customer, item and supplier IDs and names are replaced by salted keyed
  hashes (HMAC-SHA256, truncated, with a stable prefix per kind). The same
  source value always maps to the same token, so referential integrity across
  the lines file and every reference table is preserved and the engine runs
  on the anonymized copy exactly as on the original.
- Optionally, every mapped money column is scaled by a secret factor derived
  from the salt (0.5x-1.5x). Scaling happens in the SOURCE cells, so the
  engine's internal reconciliation on the anonymized copy still holds to the
  cent; only the absolute dollar level is disguised. Scaled files are written
  as plain numbers with the mapping's decimal separator (currency symbols and
  parentheses are not reproduced).
- The salt comes from a local file or the MARGIN_ENGINE_ANON_SALT environment
  variable, is never committed, and never appears in any output or manifest.
"""

from __future__ import annotations

import hmac
import os
from decimal import ROUND_HALF_EVEN, Decimal
from hashlib import sha256
from pathlib import Path

import polars as pl

from margin_engine.ingest.loader import read_table
from margin_engine.ingest.mapping import MappingError, SourceMapping

SALT_ENV = "MARGIN_ENGINE_ANON_SALT"

# Canonical fields whose SOURCE columns are hashed, with a token prefix per
# kind so anonymized data stays readable ("which column is the customer").
_HASH_FIELDS: dict[str, str] = {
    "customer_id": "C",
    "customer_name": "CN",
    "item_id": "I",
    "item_description": "IN",
    "supplier_id": "S",
    "supplier_name": "SN",
    "ship_to_id": "ST",
    "bill_to_id": "BT",
    "parent_account_id": "PA",
}

# Canonical money fields on the lines mapping (scaled when --scale is set).
_LINES_MONEY = (
    "net_amount",
    "unit_price",
    "unit_cost",
    "list_price",
    "on_invoice_discount",
    "customer_rebate_amount",
    "freight_concession",
    "restocking_fee",
    "return_freight_billed",
)
# Canonical money columns in reference tables.
_REF_MONEY = {
    "list_price",
    "amount",
    "min_amount",
    "unit_cost",
    "quoted_price",
    "close_price",
    "inbound_freight_per_unit",
    "duty_per_unit",
    "net_sales",
    "cogs",
    "returns",
}


def load_salt(salt_file: Path | None) -> bytes:
    if salt_file is not None:
        if not salt_file.exists():
            msg = f"salt file not found: {salt_file}"
            raise MappingError(msg)
        salt = salt_file.read_bytes().strip()
    else:
        env = os.environ.get(SALT_ENV, "")
        salt = env.encode("utf-8")
    if len(salt) < 16:
        msg = (
            "anonymization needs a salt of at least 16 bytes, from --salt-file "
            f"or the {SALT_ENV} environment variable. Generate one locally "
            '(e.g. `python -c "import secrets; print(secrets.token_hex(32))"`), '
            "keep it out of the repository, and keep it: the same salt maps the "
            "same source values to the same tokens."
        )
        raise MappingError(msg)
    return salt


def _token(salt: bytes, prefix: str, value: str) -> str:
    digest = hmac.new(salt, f"{prefix}|{value}".encode(), sha256).hexdigest()
    return f"{prefix}-{digest[:12]}"


def money_scale_factor(salt: bytes) -> Decimal:
    """Secret, deterministic scale in [0.5, 1.5), derived from the salt."""
    digest = hmac.new(salt, b"money-scale", sha256).hexdigest()
    return Decimal("0.5") + Decimal(int(digest[:8], 16) % 10_000) / Decimal(10_000)


def _hash_expr(col: str, prefix: str, salt: bytes) -> pl.Expr:
    return (
        pl.col(col)
        .map_elements(
            lambda v: _token(salt, prefix, str(v)) if v is not None and str(v) != "" else v,
            return_dtype=pl.String,
        )
        .alias(col)
    )


def _scale_expr(col: str, factor: Decimal, decimal_sep: str) -> pl.Expr:
    """Parse a money string exactly, scale, and re-emit as a plain number with
    the declared decimal separator, half-even to cents."""

    def scale(v: str | None) -> str | None:
        if v is None or str(v).strip() == "":
            return v
        s = str(v).strip()
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]
        # NBSP is a thousands separator in some European exports.
        for sym in ("$", "€", "£", " ", " "):  # noqa: RUF001
            s = s.replace(sym, "")
        if decimal_sep == ",":
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
        try:
            d = Decimal(s)
        except Exception:
            return v  # non-numeric cell: left as-is (validated at ingest)
        if neg:
            d = -d
        scaled = (d * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        out = f"{scaled:.2f}"
        return out.replace(".", ",") if decimal_sep == "," else out

    return pl.col(col).map_elements(scale, return_dtype=pl.String).alias(col)


def anonymize_export(
    data_dir: Path,
    mapping: SourceMapping,
    out_dir: Path,
    salt: bytes,
    scale_money: bool = False,
) -> dict[str, int]:
    """Write anonymized copies of the lines file and every declared reference
    file into out_dir. Returns per-file row counts. Column NAMES and layout
    are preserved so the same mapping runs on the anonymized copy."""
    out_dir.mkdir(parents=True, exist_ok=True)
    factor = money_scale_factor(salt) if scale_money else None
    counts: dict[str, int] = {}

    def process(file: str, exprs: list[pl.Expr]) -> None:
        path = data_dir / file
        if not path.exists():
            return
        opts, _sep = mapping.effective_input(None)
        df, _ = read_table(path, opts)
        if exprs:
            df = df.with_columns(exprs)
        df.write_csv(out_dir / file, separator=opts.delimiter)
        counts[file] = df.height

    # Lines file: hash id/name columns; scale mapped money columns.
    lm = mapping.lines
    _sep = mapping.decimal_separator
    exprs: list[pl.Expr] = []
    for canonical, prefix in _HASH_FIELDS.items():
        spec = getattr(lm, canonical, None)
        if spec is not None:
            exprs.append(_hash_expr(spec.column, prefix, salt))
    if factor is not None:
        for canonical in _LINES_MONEY:
            spec = getattr(lm, canonical, None)
            if spec is not None:
                exprs.append(_scale_expr(spec.column, factor, _sep))
    process(mapping.lines_file, exprs)

    # Reference tables: hash matching canonical columns, scale money columns.
    for _name, spec_t in mapping.reference_tables.items():
        exprs = []
        for canonical, src in spec_t.columns.items():
            if canonical in _HASH_FIELDS:
                exprs.append(_hash_expr(src, _HASH_FIELDS[canonical], salt))
            elif factor is not None and canonical in _REF_MONEY:
                exprs.append(_scale_expr(src, factor, _sep))
        process(spec_t.file, exprs)

    note = out_dir / "ANONYMIZED.md"
    note.write_text(
        "This export was anonymized by `margin-engine anonymize`.\n\n"
        "- Customer, item and supplier IDs and names are salted keyed hashes "
        "(HMAC-SHA256). The same source value maps to the same token, so the "
        "data remains internally consistent and the original mapping runs "
        "unchanged.\n"
        + (
            "- Money amounts were scaled by a secret factor derived from the "
            "salt; absolute dollar levels are disguised, internal consistency "
            "holds.\n"
            if factor is not None
            else "- Money amounts are unchanged.\n"
        )
        + "- The salt is not recorded here or anywhere in these files.\n",
        encoding="utf-8",
    )
    return counts
