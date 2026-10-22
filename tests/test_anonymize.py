"""Run 4 (27.5): anonymizer. Salted keyed hashes preserve referential
integrity; the engine runs on the anonymized copy and still reconciles; the
salt never appears in any output."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from synth.config import GeneratorConfig, Preset
from synth.generate import generate

from margin_engine.anonymize import anonymize_export, load_salt, money_scale_factor
from margin_engine.config import Rules
from margin_engine.ingest.mapping import MappingError, load_mapping
from margin_engine.ledger.pipeline import run_pipeline

MAPPING = Path(__file__).resolve().parents[1] / "config" / "mappings" / "synth_erp.yaml"
SALT = b"0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("anon")
    cfg = GeneratorConfig.for_preset(Preset.WELL_IDENTIFIED, seed=5, n_customers=12, n_items=30)
    generate(cfg, out / "world")
    return out / "world" / "erp"


def test_salt_required_and_never_weak(tmp_path: Path) -> None:
    with pytest.raises(MappingError, match="16 bytes"):
        load_salt(None)  # env unset in tests
    short = tmp_path / "salt.txt"
    short.write_bytes(b"tiny")
    with pytest.raises(MappingError, match="16 bytes"):
        load_salt(short)
    good = tmp_path / "salt2.txt"
    good.write_bytes(SALT)
    assert load_salt(good) == SALT


def test_hashing_consistent_and_salt_dependent(world: Path, tmp_path: Path) -> None:
    m = load_mapping(MAPPING)
    anonymize_export(world, m, tmp_path / "a", SALT)
    anonymize_export(world, m, tmp_path / "b", SALT)
    anonymize_export(world, m, tmp_path / "c", b"another-salt-of-32-bytes-xxxxxx!")
    a = pl.read_csv(tmp_path / "a" / m.lines_file, infer_schema=False)
    b = pl.read_csv(tmp_path / "b" / m.lines_file, infer_schema=False)
    c = pl.read_csv(tmp_path / "c" / m.lines_file, infer_schema=False)
    cust_col = m.lines.customer_id.column
    assert a[cust_col].to_list() == b[cust_col].to_list()  # same salt -> same tokens
    assert a[cust_col].to_list() != c[cust_col].to_list()  # different salt -> different
    assert all(v.startswith("C-") for v in a[cust_col].to_list())
    # Original IDs are gone everywhere.
    orig = pl.read_csv(world / m.lines_file, infer_schema=False)
    assert not set(orig[cust_col].to_list()) & set(a[cust_col].to_list())


def test_referential_integrity_engine_runs_and_reconciles(world: Path, tmp_path: Path) -> None:
    m = load_mapping(MAPPING)
    out = tmp_path / "anon"
    anonymize_export(world, m, out, SALT)
    res = run_pipeline(out, MAPPING, Rules())
    assert res.reconciliation.all_pass, res.reconciliation
    assert res.ledger.height > 0


def test_money_scaling_consistent_and_engine_reconciles(world: Path, tmp_path: Path) -> None:
    m = load_mapping(MAPPING)
    out = tmp_path / "anon_scaled"
    anonymize_export(world, m, out, SALT, scale_money=True)
    factor = money_scale_factor(SALT)
    assert factor != 1
    orig = pl.read_csv(world / m.lines_file, infer_schema=False)
    anon = pl.read_csv(out / m.lines_file, infer_schema=False)
    net_col = m.lines.net_amount.column
    o = float(orig[net_col][0])
    a = float(anon[net_col][0])
    assert a == pytest.approx(o * float(factor), abs=0.011)
    # Scaled source cells still reconcile internally.
    res = run_pipeline(out, MAPPING, Rules())
    assert res.reconciliation.all_pass


def test_salt_never_in_outputs(world: Path, tmp_path: Path) -> None:
    m = load_mapping(MAPPING)
    out = tmp_path / "anon"
    anonymize_export(world, m, out, SALT, scale_money=True)
    needle = SALT.decode()
    for f in out.rglob("*"):
        if f.is_file():
            assert needle not in f.read_text(encoding="utf-8", errors="replace"), f
