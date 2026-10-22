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
