"""Spill the ledger to Parquet between stages (Run 6, Phase 83, Finding 23).

The scale problem is not any one stage - it is coexistence. The financial
ledger is the widest frame in the engine (around 85 columns once the
waterfall, allocations and cost-to-serve have done their work), and until
this phase it stayed resident while dispersion, segmentation, guidance, the
panel, sensitivity and opportunity each built their own large frames beside
it. None of those stages reads it.

With `scale.spill_ledger` on, the ledger is written to Parquet once, after
the last stage that mutates it (cost-to-serve) and the stages that consume it
whole while it is naturally still in memory (the economic-event taxonomy, the
rebate subledger, the vendor roll-forward). It is dropped from memory for the
heavy middle of the run and read back - in full or as a projection - only
where a stage actually needs it.

**Which stages need the ledger, and how much of it:**

- *Inside the spilled window*, exactly one consumer: the per-unit
  cost-to-serve aggregation for the contribution floor, which needs five
  columns and gets five columns.
- *After the window* (leakage onward: tie-out, exports, the DuckDB store, the
  manifest's cleaning decisions, the HTML report, the web bundle), consumers
  take the full frame because they consume every column by design - an export
  that projected would be an incomplete export. The ledger is rehydrated once,
  at the start of that tail, and `PipelineResult.ledger` then satisfies the
  same contract it always did, so nothing outside `run_full` changes.

**Global sorts and partitioned stages.** The allocation stages upstream of
the spill need global order within their settlement groups (largest-remainder
cents distribution is order-sensitive, and ties break on source_line_key), and
since Run 5 they run month-partitioned: each settlement period is a complete,
independent allocation universe. Dispersion's peer statistics and the
segmentation tree need whole-column views of the *observations* frame, not
the ledger, and are untouched here. Nothing downstream of the spill re-sorts
the ledger: every consumer either aggregates (order-free) or writes it out in
the order it was stored, which is the order it was spilled in - so the spill
cannot move a tie-break.

**Parity is the contract.** The same run with the spill on and off must
produce cent-identical, byte-identical exports. `tests/test_spill.py` asserts
it on the committed sample, and the scenario matrix exercises the flag across
generated worlds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass
class SpilledLedger:
    """A ledger that lives on disk until somebody needs it.

    `read(columns)` is the whole interface: a projection costs what the
    projection costs, and nothing keeps a reference to the full frame.
    """

    path: Path
    columns: list[str]
    height: int
    _schema: dict[str, pl.DataType]

    def scan(self) -> pl.LazyFrame:
        return pl.scan_parquet(self.path)

    def read(self, columns: Sequence[str] | None = None) -> pl.DataFrame:
        lf = self.scan()
        if columns is not None:
            missing = [c for c in columns if c not in self.columns]
            if missing:
                msg = (
                    f"spilled ledger has no column(s) {missing}; a projection that "
                    "guesses wrong must fail here, loudly, not produce nulls"
                )
                raise KeyError(msg)
            lf = lf.select(list(columns))
        return lf.collect()

    def empty_frame(self) -> pl.DataFrame:
        """A zero-row frame with the ledger's schema.

        It stands in for the ledger while it is on disk, so code that only
        asks "does this column exist" keeps getting the true answer, and code
        that actually reads rows fails visibly instead of silently seeing an
        empty ledger.
        """
        return pl.DataFrame(schema=self._schema)


def spill_ledger(frame: pl.DataFrame, path: Path) -> SpilledLedger:
    """Write the ledger to Parquet and hand back the handle.

    The write preserves row order, which is what makes the spill invisible to
    everything order-sensitive downstream: the frame that comes back from
    `read()` is the frame that went in.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    return SpilledLedger(
        path=path,
        columns=list(frame.columns),
        height=frame.height,
        _schema=dict(frame.schema),
    )
