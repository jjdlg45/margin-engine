"""Scenario matrix (Run 4, 27.7): many generated worlds, invariants that must
hold WITHOUT knowing the true answer.

Dimensions (sampled deterministically with a fixed seed): world size,
sparsity/demand preset, missing-cost rate, returns rate, credit-memo rate,
currency mix, ERP format profile, optional tables present or absent.

Invariants per world:
- Reconciliation exact to the cent (every allocation sums to its source -
  these are the pipeline's reconciliation checks).
- Fail-closed: when the planted truth is not identifiable, no segment reaches
  `causal_elasticity_supported`. The fail-closed presets are weak_instrument,
  invalid_instrument, price_constant, sparse and competitor_comovement. The
  other misspecified presets (heterogeneous_elasticity, nonconstant_elasticity,
  persistent_demand) recover a well-defined average estimand and are REPORTED
  as informational per spec 24.9, so causal support there is not a violation.
- Determinism: running the same world twice gives byte-identical parquet
  exports (report.html and the manifest carry timestamps by design).
- Format invariance: the same economic data in a different ERP format profile
  gives identical ledger totals.
- Monotonicity: raising the minimum margin never lowers any floor; a cost
  increase with no pass-through never raises contribution at fixed price.
- No crash: any unhandled exception in a world run is an invariant failure;
  deliberately invalid inputs must fail with an actionable MappingError or
  MoneyParseError, never a raw stack trace.
"""

from __future__ import annotations

import csv
import io
import random
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from synth.config import GeneratorConfig, MessRates, Preset
from synth.generate import generate

from margin_engine.config import Rules, load_rules
from margin_engine.guidance.bands import invoice_floor_cents, pocket_floor_cents
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.report.full_run import FullRunResult, run_full
from margin_engine.scenarios.tariff import CostShock, run_cost_scenario
from margin_engine.sensitivity.estimate import STATUS_CAUSAL

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"
RULES = REPO / "config" / "rules.yaml"

FAIL_CLOSED_PRESETS = {
    Preset.WEAK_INSTRUMENT,
    Preset.INVALID_INSTRUMENT,
    Preset.PRICE_CONSTANT,
    Preset.SPARSE,
    Preset.COMPETITOR_COMOVEMENT,
}

ALL_PRESETS = [
    Preset.WELL_IDENTIFIED,
    Preset.WEAK_INSTRUMENT,
    Preset.INVALID_INSTRUMENT,
    Preset.SPARSE,
    Preset.PRICE_CONSTANT,
    Preset.REALISTIC_NOISE,
    Preset.PERSISTENT_DEMAND,
    Preset.HETEROGENEOUS_ELASTICITY,
    Preset.NONCONSTANT_ELASTICITY,
    Preset.COMPETITOR_COMOVEMENT,
]

FORMAT_PROFILES = (
    "plain",
    "semicolon_comma",
    "win1252_currency",
    "parens_negatives",
    "bom_padded_blank",
    "xlsx",
)

MONEY_COLS = ["EXT_NET", "UNIT_PRC", "LIST_PRC", "UNIT_CST", "DISC_AMT", "REB_AMT", "FRT_CONC"]
_DECIMAL = re.compile(r"-?\d+\.\d+")


@dataclass(frozen=True)
class WorldSpec:
    idx: int
    preset: Preset
    n_customers: int
    n_items: int
    months: int
    missing_cost_rate: float
    return_rate: float
    credit_memo_rate: float
    currency_mix: str  # "single" | "multi"
    format_profile: str
    optional_tables: str  # "all" | "minimal"
    seed: int

    def label(self) -> str:
        return (
            f"w{self.idx:03d}-{self.preset.value}-{self.n_customers}x{self.n_items}"
            f"-{self.format_profile}-{self.currency_mix}-{self.optional_tables}"
        )


def sample_worlds(
    n: int, sizes: tuple[tuple[int, int], ...], matrix_seed: int = 20277
) -> list[WorldSpec]:
    """Deterministic sample: presets and format profiles round-robin (so every
    one appears), the remaining dimensions drawn from a seeded RNG."""
    rng = random.Random(matrix_seed)
    specs: list[WorldSpec] = []
    for i in range(n):
        currency_mix = rng.choice(("single", "single", "multi"))
        optional_tables = rng.choice(("all", "all", "minimal"))
        # FX conversion requires the fx reference table; a multi-currency world
        # without it fails by design (actionable error), covered separately.
        if currency_mix == "multi":
            optional_tables = "all"
        size = rng.choice(sizes)
        specs.append(
            WorldSpec(
                idx=i,
                preset=ALL_PRESETS[i % len(ALL_PRESETS)],
                n_customers=size[0],
                n_items=size[1],
                months=rng.choice((12, 18)),
                missing_cost_rate=rng.choice((0.0, 0.01, 0.2, 0.5)),
                return_rate=rng.choice((0.0, 0.04, 0.12)),
                credit_memo_rate=rng.choice((0.0, 0.02, 0.08)),
                currency_mix=currency_mix,
                format_profile=FORMAT_PROFILES[i % len(FORMAT_PROFILES)],
                optional_tables=optional_tables,
                seed=31000 + i,
            )
        )
    return specs


# ---------------------------------------------------------------------------
# World construction
# ---------------------------------------------------------------------------


def _read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _write_rows(
    path: Path,
    header: list[str],
    rows: list[list[str]],
    delimiter: str = ",",
    encoding: str = "utf-8",
) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path.write_bytes(buf.getvalue().encode(encoding))


def _transform_money(path: Path, cell_fn: Any) -> None:
    header, rows = _read_rows(path)
    idx = [header.index(c) for c in MONEY_COLS if c in header]
    for row in rows:
        for i in idx:
            if row[i]:
                row[i] = cell_fn(row[i])
    _write_rows(path, header, rows)


def apply_format_profile(erp: Path, mapping: dict[str, Any], profile: str) -> dict[str, Any]:
    """Transform the erp dir IN PLACE into the profile's physical format and
    return the adjusted mapping dict. Economics are untouched."""
    lines = erp / "invoice_lines.csv"
    if profile == "plain":
        return mapping
    if profile == "semicolon_comma":
        for f in sorted(erp.glob("*.csv")):
            header, rows = _read_rows(f)
            rows = [
                [_DECIMAL.sub(lambda m: m.group().replace(".", ","), v) for v in row]
                for row in rows
            ]
            _write_rows(f, header, rows, delimiter=";")
        mapping["input"] = {**mapping.get("input", {}), "delimiter": ";"}
        mapping["decimal_separator"] = ","
        return mapping
    if profile == "win1252_currency":
        _transform_money(lines, lambda v: f"${v}")
        header, rows = _read_rows(lines)
        header.append("NOTES")
        for row in rows:
            row.append("entregado – ok")  # noqa: RUF001 - cp1252 bytes are the point
        _write_rows(lines, header, rows, encoding="windows-1252")
        mapping["input"] = {
            **mapping.get("input", {}),
            "encoding": "windows-1252",
            "currency_symbols": ["$"],
        }
        return mapping
    if profile == "parens_negatives":
        _transform_money(lines, lambda v: f"({v[1:]})" if v.startswith("-") else v)
        mapping["input"] = {**mapping.get("input", {}), "negative_format": "parentheses"}
        return mapping
    if profile == "bom_padded_blank":
        header, rows = _read_rows(lines)
        n_cols = len(header)
        rows = [[f" {v} " for v in row] for row in rows]
        rows = [*rows[:5], [""] * n_cols, [" "] * n_cols, *rows[5:]]
        _write_rows(lines, [f" {h} " for h in header], rows)
        lines.write_bytes(b"\xef\xbb\xbf" + lines.read_bytes() + b"\n \n")
        return mapping
    if profile == "xlsx":
        from openpyxl import Workbook

        header, rows = _read_rows(lines)
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(header)
        for row in rows:
            ws.append(row)
        wb.save(erp / "invoice_lines.xlsx")
        lines.unlink()
        mapping["lines_file"] = "invoice_lines.xlsx"
        return mapping
    msg = f"unknown format profile {profile}"
    raise ValueError(msg)


def build_world(spec: WorldSpec, tmp: Path) -> tuple[Path, Path, Path, Path, Path]:
    """Generate the world and its format variant.

    Returns (erp_fmt, mapping_fmt, erp_plain, mapping_plain, rules_path)."""
    overrides: dict[str, Any] = {
        "n_customers": spec.n_customers,
        "n_items": spec.n_items,
        "months": spec.months,
        "mess": MessRates(
            missing_cost_rate=spec.missing_cost_rate,
            return_rate=spec.return_rate,
            credit_memo_rate=spec.credit_memo_rate,
        ),
    }
    if spec.currency_mix == "multi":
        overrides["currency_shares"] = {"USD": 0.6, "EUR": 0.25, "GBP": 0.15}
    cfg = GeneratorConfig.for_preset(spec.preset, seed=spec.seed, **overrides)
    generate(cfg, tmp / "world")
    erp_plain = tmp / "world" / "erp"

    mapping_dict = yaml.safe_load(MAPPING.read_text(encoding="utf-8"))
    if spec.optional_tables == "minimal":
        for name, tspec in list(mapping_dict.get("reference_tables", {}).items()):
            mapping_dict["reference_tables"].pop(name)
            f = erp_plain / tspec["file"]
            if f.exists():
                f.unlink()
    mapping_plain = tmp / "mapping_plain.yaml"
    mapping_plain.write_text(yaml.safe_dump(mapping_dict), encoding="utf-8")

    erp_fmt = tmp / "erp_fmt"
    shutil.copytree(erp_plain, erp_fmt)
    mapping_fmt_dict = apply_format_profile(
        erp_fmt, yaml.safe_load(mapping_plain.read_text(encoding="utf-8")), spec.format_profile
    )
    mapping_fmt = tmp / "mapping_fmt.yaml"
    mapping_fmt.write_text(yaml.safe_dump(mapping_fmt_dict), encoding="utf-8")

    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    rules.setdefault("segmentation", {})["bootstrap_resamples"] = 3
    rules.setdefault("sensitivity", {})["placebo_draws"] = 2
    rules_path = tmp / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rules), encoding="utf-8")
    return erp_fmt, mapping_fmt, erp_plain, mapping_plain, rules_path


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def _ledger_totals(ledger: pl.DataFrame) -> dict[str, int]:
    return {
        "n_lines": ledger.height,
        "net": int(ledger["net_amount_cents"].sum()),
        "pocket": int(ledger["pocket_revenue_cents"].sum()),
        "cost": int(ledger["product_cost_cents"].drop_nulls().sum()),
    }


def _check_determinism(out1: Path, out2: Path) -> list[str]:
    fails: list[str] = []
    files1 = sorted(p.name for p in out1.glob("*.parquet"))
    files2 = sorted(p.name for p in out2.glob("*.parquet"))
    if files1 != files2:
        return [f"determinism: export sets differ ({files1} vs {files2})"]
    for name in files1:
        if (out1 / name).read_bytes() != (out2 / name).read_bytes():
            fails.append(f"determinism: {name} differs between identical runs")
    return fails


def _check_floor_monotonicity(guidance: pl.DataFrame, rules: Rules) -> list[str]:
    g = guidance.filter(pl.col("unit_cost_cents").is_not_null() & (pl.col("unit_cost_cents") > 0))
    if not g.height:
        return []
    m_bp = int(rules.price_guidance.min_pocket_gross_margin * 10_000)
    rounding = rules.price_guidance.floor_rounding
    checked = g.select(
        pocket_floor_cents(pl.col("unit_cost_cents"), m_bp, rounding).alias("p_lo"),
        pocket_floor_cents(pl.col("unit_cost_cents"), m_bp + 200, rounding).alias("p_hi"),
        invoice_floor_cents(
            pl.col("unit_cost_cents"),
            m_bp,
            pl.col("fixed_deduction_cents"),
            pl.col("deduction_bp"),
            rounding,
        ).alias("i_lo"),
        invoice_floor_cents(
            pl.col("unit_cost_cents"),
            m_bp + 200,
            pl.col("fixed_deduction_cents"),
            pl.col("deduction_bp"),
            rounding,
        ).alias("i_hi"),
    )
    fails: list[str] = []
    if checked.filter(pl.col("p_hi") < pl.col("p_lo")).height:
        fails.append("monotonicity: raising min margin lowered a pocket floor")
    if checked.filter(pl.col("i_hi") < pl.col("i_lo")).height:
        fails.append("monotonicity: raising min margin lowered an invoice floor")
    return fails


def _check_cost_increase(res: FullRunResult, rules: Rules) -> list[str]:
    if res.panel is None or not res.guidance.height or "item_category" not in res.panel.columns:
        return []
    cats = [c for c in res.panel["item_category"].unique().to_list() if c is not None]
    if not cats:
        return []
    import datetime

    shock = CostShock(
        name="matrix_invariant",
        scope="category",
        scope_value=sorted(cats)[0],
        pct_increase=0.10,
        effective=datetime.date(2024, 1, 1),
    )
    sc = run_cost_scenario(res.panel, res.guidance, res.sensitivity, shock, rules)
    none_rows = sc.filter(pl.col("strategy") == "none")
    fails: list[str] = []
    for r in none_rows.to_dicts():
        if (r["incremental_cost_cents"] or 0) < 0:
            fails.append("monotonicity: negative incremental cost on a positive cost shock")
        if (r["recovered_cents"] or 0) != 0:
            fails.append("monotonicity: zero pass-through recovered a nonzero amount")
        eff = r["contribution_effect_cents"]
        if eff is not None and eff > 0:
            fails.append(
                "monotonicity: a cost increase at fixed price raised expected contribution"
            )
    return fails


def run_world(spec: WorldSpec, tmp: Path) -> dict[str, Any]:
    """Run one world and every invariant. Returns a result record with the
    spec (reproducible) and the list of invariant failures (empty = pass)."""
    failures: list[str] = []
    erp_fmt, mapping_fmt, erp_plain, mapping_plain, rules_path = build_world(spec, tmp)
    rules_obj = load_rules(rules_path)

    res1 = run_full(erp_fmt, mapping_fmt, rules_path, tmp / "out1")
    if not res1.pipeline.reconciliation.all_pass:
        failed = [c["check"] for c in res1.pipeline.reconciliation.checks if not c["pass"]]
        failures.append(f"reconciliation: {failed}")

    if spec.preset in FAIL_CLOSED_PRESETS and res1.sensitivity.height:
        causal = res1.sensitivity.filter(pl.col("status") == STATUS_CAUSAL)
        if causal.height:
            failures.append(
                f"fail_closed: {causal.height} segment(s) reached causal support on "
                f"the unidentifiable preset {spec.preset.value}"
            )

    run_full(erp_fmt, mapping_fmt, rules_path, tmp / "out2")
    failures.extend(_check_determinism(tmp / "out1", tmp / "out2"))

    if spec.format_profile != "plain":
        plain_res = run_pipeline(erp_plain, mapping_plain, rules_obj)
        t_plain = _ledger_totals(plain_res.ledger)
        t_fmt = _ledger_totals(res1.pipeline.ledger)
        if t_plain != t_fmt:
            failures.append(
                f"format_invariance: {spec.format_profile} totals {t_fmt} != plain {t_plain}"
            )

    failures.extend(_check_floor_monotonicity(res1.guidance, rules_obj))
    failures.extend(_check_cost_increase(res1, rules_obj))

    return {"spec": {**asdict(spec), "preset": spec.preset.value}, "failures": failures}
