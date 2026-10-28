"""Run 6, Phase 83 (Finding 23): the spill path changes memory, not numbers.

The ledger is the widest frame in the engine, and until this phase it stayed
resident through stages that never read it. Spilling it to Parquet for the
analytic middle of the run is only acceptable if it is invisible everywhere
else: same exports to the byte, same ledger to the cent, same tie-breaks.
That is what these tests hold it to.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from margin_engine.ledger.spill import spill_ledger
from margin_engine.memlog import StagePeaks
from margin_engine.report.full_run import run_full

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample" / "erp"
MAPPING = REPO / "config" / "mappings" / "synth_erp.yaml"
RULES = REPO / "config" / "rules.yaml"


# ---------------------------------------------------------------------------
# the handle itself
# ---------------------------------------------------------------------------


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "source_line_key": ["L3", "L1", "L2"],  # deliberately unsorted
            "net_amount_cents": [1250, -99, 0],
            "transaction_type": pl.Series(["sale", "return", "sale"], dtype=pl.Categorical),
            "quantity_norm": [2.0, None, 0.5],
            "invoice_date": pl.Series(["2024-01-02", "2024-01-03", "2024-01-04"]).str.to_date(),
        }
    )


def test_what_comes_back_is_what_went_in(tmp_path: Path) -> None:
    """Values, dtypes (categoricals included) and - the part a tie-break
    depends on - row order."""
    original = _frame()
    spilled = spill_ledger(original, tmp_path / "work" / "ledger.parquet")
    assert_frame_equal(spilled.read(), original)
    assert spilled.read()["source_line_key"].to_list() == ["L3", "L1", "L2"]
    assert spilled.height == 3
    assert spilled.columns == original.columns


def test_a_projection_reads_only_what_was_asked(tmp_path: Path) -> None:
    original = _frame()
    spilled = spill_ledger(original, tmp_path / "ledger.parquet")
    got = spilled.read(["net_amount_cents", "source_line_key"])
    assert got.columns == ["net_amount_cents", "source_line_key"]
    assert_frame_equal(got, original.select("net_amount_cents", "source_line_key"))


def test_a_wrong_projection_fails_loudly_not_with_nulls(tmp_path: Path) -> None:
    spilled = spill_ledger(_frame(), tmp_path / "ledger.parquet")
    with pytest.raises(KeyError, match="no column"):
        spilled.read(["net_amount_cents", "a_column_nobody_made"])


def test_the_placeholder_keeps_the_schema_and_none_of_the_rows(tmp_path: Path) -> None:
    """Code that asks 'does this column exist' keeps getting the true answer
    while the ledger is on disk; code that reads rows sees none and fails
    visibly rather than computing quietly on an empty ledger."""
    spilled = spill_ledger(_frame(), tmp_path / "ledger.parquet")
    empty = spilled.empty_frame()
    assert empty.height == 0
    assert empty.schema == _frame().schema


# ---------------------------------------------------------------------------
# parity on the committed sample: the whole point
# ---------------------------------------------------------------------------


def _export_hashes(out: Path) -> dict[str, str]:
    """Every CSV and Parquet export. The manifest (timestamps, the spill
    note), the report (embeds the manifest), DuckDB and the spill's own work
    file are excluded for the same reasons the determinism tests exclude
    them; every financial figure lives in the hashed files."""
    return {
        str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out.rglob("*"))
        if p.is_file()
        and p.suffix in (".csv", ".parquet")
        and "work" not in p.relative_to(out).parts
    }


@pytest.fixture(scope="module")
def both_runs(tmp_path_factory: pytest.TempPathFactory) -> tuple:
    base = tmp_path_factory.mktemp("spill_parity")
    in_memory = run_full(SAMPLE, MAPPING, RULES, base / "in_memory", validate=False)
    spilled = run_full(SAMPLE, MAPPING, RULES, base / "spilled", validate=False, spill=True)
    return in_memory, spilled, base


def test_every_export_is_byte_identical_across_the_two_paths(both_runs: tuple) -> None:
    _in_memory, _spilled, base = both_runs
    a = _export_hashes(base / "in_memory")
    b = _export_hashes(base / "spilled")
    assert a.keys() == b.keys()
    differing = [k for k in a if a[k] != b[k]]
    assert not differing, f"spill path changed exports: {differing}"
    assert len(a) >= 15


def test_the_rehydrated_ledger_is_the_ledger(both_runs: tuple) -> None:
    in_memory, spilled, _ = both_runs
    assert_frame_equal(spilled.pipeline.ledger, in_memory.pipeline.ledger)
    assert spilled.ledger_spill_path is not None
    assert spilled.ledger_spill_path.exists()
    assert in_memory.ledger_spill_path is None


def test_the_run_says_it_spilled_and_the_in_memory_run_does_not(both_runs: tuple) -> None:
    in_memory, spilled, _ = both_runs
    assert any("ledger spilled to Parquet" in n for n in spilled.pipeline.notes)
    assert not any("spilled" in n for n in in_memory.pipeline.notes)


def test_downstream_frames_match_to_the_cent(both_runs: tuple) -> None:
    """Leakage, guidance and opportunities are built from reads that crossed
    the spill boundary; identical inputs must mean identical CONTENT.

    Row order inside these frames was never a promise - group_by order varies
    run to run even without the spill, which is why every export sorts before
    writing - so the comparison sorts by key the way the exports do.
    """
    in_memory, spilled, _ = both_runs

    def canon(df: pl.DataFrame) -> pl.DataFrame:
        keys = [
            c
            for c in df.columns
            if df.schema[c] in (pl.String, pl.Categorical) or c.endswith("_id")
        ]
        return df.with_columns(pl.col(k).cast(pl.String) for k in keys).sort(keys or df.columns)

    assert_frame_equal(canon(spilled.leakage), canon(in_memory.leakage))
    assert_frame_equal(canon(spilled.guidance), canon(in_memory.guidance))
    assert_frame_equal(canon(spilled.opportunities), canon(in_memory.opportunities))


def test_stage_peaks_reach_the_manifest(both_runs: tuple) -> None:
    """Peak memory per stage is a measurement with a thread behind it, not the
    last RSS reading standing. Absent psutil it is an empty dict, which is a
    statement, not a zero."""
    in_memory, _, _ = both_runs
    peaks = in_memory.manifest["stage_peak_rss_gb"]
    if not StagePeaks().available:
        assert peaks == {}
        return
    for stage in ("pipeline_done", "segmentation", "sensitivity", "export_files_written"):
        assert stage in peaks, f"no peak recorded for {stage}"
        assert peaks[stage] > 0


# ---------------------------------------------------------------------------
# the sampler on its own
# ---------------------------------------------------------------------------


def test_the_sampler_sees_a_balloon_a_boundary_reading_would_miss() -> None:
    if not StagePeaks().available:
        pytest.skip("psutil unavailable")
    import time

    peaks = StagePeaks(interval_s=0.01).start()
    peaks.mark("quiet")
    big = list(range(3_000_000))  # ~100 MB for a beat
    time.sleep(0.15)
    del big
    time.sleep(0.05)
    peaks.mark("balloon")
    out = peaks.stop()
    assert out["balloon"] > out["quiet"], out
    assert out["quiet"] > 0  # the boundary reading, even with no sample landed
