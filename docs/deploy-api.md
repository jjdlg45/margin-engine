# Deploying the BFF API (documented, not executed)

Run 4 (30.8). This documents how the owner COULD host `api/` on a free tier.
Nothing here has been deployed; no accounts were created. The GitHub Pages
demo stays static either way — API mode is opt-in per build.

## What the service needs

- Python 3.12 + `uv sync --group api` (FastAPI, uvicorn).
- Start command: `uv run uvicorn api.main:app --host 0.0.0.0 --port $PORT`.
- A bundle to serve: either bake `web/public/data/` into the image/checkout,
  or enable the replay feed, which builds bundles itself.
- Disk for DuckDB state (`MARGIN_API_DB`). On hosts without persistent disk
  the service rebuilds state on each cold start — acceptable for the demo
  (the replay feed simply restarts from day one).

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MARGIN_API_BUNDLE_DIR` | `web/public/data` | Exported bundle to serve at startup |
| `MARGIN_API_DB` | `.api_state/api.duckdb` | DuckDB state (bundle versions, replay bookkeeping) |
| `MARGIN_API_RATE_LIMIT` / `MARGIN_API_RATE_BURST` | 240 / 60 | Inbound per-client rate limit |
| `MARGIN_API_REPLAY` | 0 | 1 enables the replay feed (30.4) |
| `MARGIN_API_REPLAY_SPEED` | 60 | Seconds of wall time per replayed data-day |
| `MARGIN_API_REPLAY_SOURCE` | committed sample | Replay source dir (full Online Retail II via `scripts/download_online_retail.py`) |
| `MARGIN_API_FX_REFRESH_S` / `MARGIN_API_PPI_REFRESH_S` | daily / monthly | Connector refresh cadence |

The scheduler (FX daily, PPI monthly, replay per tick) runs INSIDE the API
process — no separate worker is needed, which matters on free tiers that
allow exactly one service.

## Option A — Render free web service

Verified against Render's own docs (docs.render.com/free), 2026-10-22:
**750 free instance-hours per workspace per month**; a free service **spins
down after 15 minutes without inbound traffic** (cold start ~1 minute); **no
persistent disk** on free instances; ~512 MB RAM (third-party figure — check
the pricing page).

- Create a Web Service from the GitHub repo; runtime Python; build command
  `pip install uv && uv sync --group api`; start command as above.
- Set the env vars; keep `MARGIN_API_REPLAY_SOURCE` at the committed sample
  (the full 1M-line dataset re-runs the engine each tick — too heavy for
  512 MB; the sample world runs in seconds).
- Consequences of the free tier, stated: the replay feed pauses whenever the
  service spins down (resuming where DuckDB state allows — with no disk, it
  restarts from day one on every cold start, honestly labeled by the
  "Data as of" indicator); the first request after idle takes ~1 minute.

## Option B — Oracle Cloud Always Free VM

Verified 2026-10-22 from press/community coverage of Oracle's notice (the
limits were REDUCED in 2026): Always Free now allows up to **2 Ampere A1
OCPUs and 12 GB RAM** total (plus 2 AMD micro VMs), free indefinitely;
account sign-up requires card verification. Check Oracle's official Always
Free docs before relying on the exact numbers — Oracle changed them once
this year already.

- Provision one A1 VM (e.g. 2 OCPU / 8 GB), install Python 3.12 + uv + node,
  clone the repo, `uv sync --group api`.
- Run the API under systemd with the env vars above; this VM is strong
  enough to replay the FULL Online Retail II feed (the 1M-line engine run
  takes ~2.5 minutes at 4.2 GB peak; set `MARGIN_API_REPLAY_SPEED` ≥ 300).
- Persistent disk means DuckDB state survives restarts: the replay resumes
  from the last completed day.
- Put the API behind the VM's firewall + a reverse proxy (caddy/nginx) for
  TLS; the API's own rate limiting still applies per client.

## Switching the hosted UI to API mode

The UI build decides the data source:

```
VITE_DATA_SOURCE=http VITE_API_BASE_URL=https://<api-host>/api npm run build
```

Cross-origin setups need either a same-origin proxy in front of the UI or
CORS enabled on the API host; the demo setups above assume the proxy (the
vite dev server and `scripts/live_mode.py` already do this locally). The
GitHub Pages build keeps `VITE_DATA_SOURCE` unset, so the public demo remains
fully static and can never silently depend on a server.
