"""Optional stage-level memory logging (Run 4, 27.2 tuning).

Enabled by MARGIN_ENGINE_RSS_LOG=1. Uses psutil when available; silently does
nothing otherwise, so production runs carry no dependency and no cost.
"""

from __future__ import annotations

import os


def rss_log(stage: str) -> None:
    if os.environ.get("MARGIN_ENGINE_RSS_LOG") != "1":
        return
    try:
        import psutil  # type: ignore[import-untyped]

        rss = psutil.Process().memory_info().rss / 1024**3
        print(f"[rss] {stage}: {rss:.2f} GB", flush=True)
    except Exception:
        return


class StagePeaks:
    """Peak RSS per stage, sampled rather than guessed (Run 6, Phase 83).

    `rss_log` records memory AT stage boundaries, which misses the shape that
    matters: a stage that balloons and frees before its boundary reads as
    cheap. This samples on a daemon thread every tenth of a second and keeps
    the maximum seen between `mark()` calls, so "peak memory per stage" is a
    measurement, not the last value standing.

    Does nothing (and costs nothing) when psutil is unavailable; the results
    then say so rather than reporting zeros that look like measurements.
    """

    def __init__(self, interval_s: float = 0.1) -> None:
        self.interval_s = interval_s
        self.peaks: dict[str, float] = {}
        self._current_peak = 0.0
        self._stop = False
        import threading

        self._thread: threading.Thread | None = None
        self.available = False
        try:
            import psutil

            self._proc = psutil.Process()
            self.available = True
        except Exception:
            self._proc = None

    def start(self) -> StagePeaks:
        if not self.available:
            return self
        import threading

        assert self._proc is not None

        def _run() -> None:
            while not self._stop:
                try:
                    rss = self._proc.memory_info().rss / 1024**3
                except Exception:
                    break
                if rss > self._current_peak:
                    self._current_peak = rss
                import time as _t

                _t.sleep(self.interval_s)

        self._thread = threading.Thread(target=_run, name="stage-peaks", daemon=True)
        self._thread.start()
        return self

    def mark(self, stage: str) -> None:
        """Label the window that just ENDED.

        run_full calls this at each stage boundary with the name of the stage
        that just ran, so the samples between two marks belong to the first
        mark's name. One direct reading is folded in as well: a stage fast
        enough to fall between two thread samples still records its boundary
        memory instead of a zero that looks like a measurement.
        """
        if not self.available:
            return
        try:
            now = self._proc.memory_info().rss / 1024**3
        except Exception:
            now = 0.0
        peak = max(self._current_peak, now)
        self.peaks[stage] = round(max(self.peaks.get(stage, 0.0), peak), 3)
        self._current_peak = 0.0

    def stop(self) -> dict[str, float]:
        if not self.available:
            return {}
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        return dict(self.peaks)
