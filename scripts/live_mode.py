"""Run 4 (30.6): live mode with one command (Docker-free variant; the spec
allows a documented uv + npm script where Docker is unavailable).

    uv run python scripts/live_mode.py [--speed SECONDS_PER_DAY] [--source DIR]

Starts, and stops together on Ctrl+C:
- the BFF API (uvicorn, http://127.0.0.1:8321) with the replay feed enabled:
  the Online Retail II data (the committed sample by default; pass --source
  with the full converted dataset dir for the real feed) is replayed one
  business day per tick, the engine re-runs and the bundle refreshes;
- the UI dev server (http://127.0.0.1:4174) in API mode, /api proxied to the
  BFF same-origin.

Scheduled refreshes run inside the API process (FX daily, PPI monthly, via
the Phase 29 connectors and their caches/fixtures). The UI header shows
"Data as of <day>" and labels any stale section.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=60.0, help="seconds per replayed data-day")
    ap.add_argument(
        "--source",
        type=Path,
        default=REPO / "data" / "sample_online_retail",
        help="replay source dir (lines + reference CSVs)",
    )
    args = ap.parse_args()

    api_env = {
        **os.environ,
        "MARGIN_API_REPLAY": "1",
        "MARGIN_API_REPLAY_SPEED": str(args.speed),
        "MARGIN_API_REPLAY_SOURCE": str(args.source),
    }
    ui_env = {
        **os.environ,
        "VITE_DATA_SOURCE": "http",
        "VITE_API_PROXY_TARGET": "http://127.0.0.1:8321",
    }
    api = subprocess.Popen(
        ["uv", "run", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8321"],
        cwd=REPO,
        env=api_env,
    )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    ui = subprocess.Popen(
        [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", "4174", "--strictPort"],
        cwd=REPO / "web",
        env=ui_env,
    )
    print("\nLive mode: UI http://127.0.0.1:4174  |  API http://127.0.0.1:8321/api/health")
    print(f"Replay: one data-day every {args.speed:.0f}s from {args.source}\nCtrl+C stops both.")

    def shutdown(*_a: object) -> None:
        for p in (ui, api):
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    api.wait()
    ui.terminate()


if __name__ == "__main__":
    main()
