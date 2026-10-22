"""Run 4 (27.7): scenario-matrix invariant tests.

Fast tier: ~30 small worlds in the default suite and CI, one test per world
(a failure names the world's parameters so it can be reproduced). Full tier:
several hundred worlds behind `-m slow`, summarized into validation.json by
scripts/run_matrix_full.py.

Separately: deliberately invalid inputs must fail with an actionable
MappingError / MoneyParseError, never a raw stack trace.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from margin_engine.config import load_rules
from margin_engine.ingest.mapping import MappingError
from margin_engine.ledger.pipeline import run_pipeline
from margin_engine.money import MoneyParseError
from tests.matrix_worlds import (
    MAPPING,
    RULES,
    WorldSpec,
    build_world,
    run_world,
    sample_worlds,
)

FAST_SIZES = ((20, 40), (30, 60), (50, 80))
FAST_WORLDS = sample_worlds(30, FAST_SIZES)


@pytest.mark.parametrize("spec", FAST_WORLDS, ids=lambda s: s.label())
def test_matrix_fast_world(spec: WorldSpec, tmp_path: Path) -> None:
    result = run_world(spec, tmp_path)
    assert not result["failures"], (
        f"invariant failure(s) on {spec.label()} (reproduce with this spec: "
        f"{result['spec']}): {result['failures']}"
    )


@pytest.mark.slow
def test_matrix_full_tier(tmp_path: Path) -> None:
    """Several hundred worlds; run once per phase via scripts/run_matrix_full.py
    (which also records the summary in validation.json). Any failure lists the
    world parameters for reproduction."""
    from scripts.run_matrix_full import FULL_SIZES, N_FULL

    specs = sample_worlds(N_FULL, FULL_SIZES, matrix_seed=20278)
    bad = []
    for spec in specs:
        d = tmp_path / spec.label()
        d.mkdir()
        result = run_world(spec, d)
        if result["failures"]:
            bad.append(result)
    assert not bad, f"{len(bad)} world(s) failed invariants: {bad}"


# ---------------------------------------------------------------------------
# No-crash: invalid input fails with an actionable message
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tiny_world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    tmp = tmp_path_factory.mktemp("invalid_input")
    spec = sample_worlds(1, ((15, 30),))[0]
    erp_fmt, mapping_fmt, _erp_plain, _mapping_plain, rules_path = build_world(spec, tmp)
    return erp_fmt, mapping_fmt, rules_path


def test_wrong_delimiter_is_actionable(tiny_world: tuple[Path, Path, Path]) -> None:
    erp, mapping_path, rules_path = tiny_world
    m = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    m["input"] = {**m.get("input", {}), "delimiter": ";"}
    bad = mapping_path.parent / "bad_delim.yaml"
    bad.write_text(yaml.safe_dump(m), encoding="utf-8")
    with pytest.raises(MappingError, match=r"input\.delimiter|delimiter"):
        run_pipeline(erp, bad, load_rules(rules_path))


def test_garbage_bytes_are_actionable(tmp_path: Path) -> None:
    erp = tmp_path / "erp"
    erp.mkdir()
    (erp / "invoice_lines.csv").write_bytes(b"\xff\xfe\x00\x01 not a csv \x80\x81")
    with pytest.raises(MappingError):
        run_pipeline(erp, MAPPING, load_rules(RULES))


def test_malformed_money_is_actionable(tiny_world: tuple[Path, Path, Path]) -> None:
    erp, mapping_path, rules_path = tiny_world
    import csv

    lines = erp / "invoice_lines.csv"
    with open(lines, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    i = header.index("EXT_NET")
    body[0][i] = "12,34,56abc"
    import io

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(body)
    lines.write_text(buf.getvalue(), encoding="utf-8")
    with pytest.raises((MoneyParseError, MappingError)):
        run_pipeline(erp, mapping_path, load_rules(rules_path))
