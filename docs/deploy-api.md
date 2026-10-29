# Deploying the BFF API (documented, not executed)

Run 4 (30.8). This documents how the owner COULD host `api/` on a free tier.
Nothing here has been deployed; no accounts were created. The GitHub Pages
demo stays static either way — API mode is opt-in per build.

## Local mode and production mode (Run 6, Phase 82)

`MARGIN_API_MODE` decides who says who you are, and it is the first thing to
set before putting this service anywhere other people can reach.

**`local` (the default).** The client sends `X-User` naming a user from
`MARGIN_API_USERS`. That is **identity, not authentication**: anyone who can
reach the port can be anyone. It is right for one person on one machine and
wrong for everything else. The README documents it as such, and `/api/auth/me`
says so in its own response.

**`production`.** Client-supplied identity is **rejected** - `X-User` returns
400, not a silent fallback, so a caller cannot believe it worked. Identity
comes from a bearer token the server issued:

```
POST /api/auth/login   {"user_id": "...", "password": "..."}  -> {"token": ...}
GET  /api/auth/me      Authorization: Bearer <token>
POST /api/auth/logout  Authorization: Bearer <token>
```

Accounts come from `MARGIN_API_ACCOUNTS`, one per line (or semicolon
separated):

```
user_id|display name|role+role|password_hash
```

Generate a hash without putting the password in your shell history:

```
uv run python -m api.auth hash
```

Roles are `analyst`, `approver`, `viewer`. The **user id is immutable and is
what every authorisation decision uses**; the display name is for reading and
changing it changes nothing else. That separation is the point: a
separation-of-duties rule written against display names is a rule about
strings, and the person it constrains can edit it.

### What the service refuses to start without

Production mode fails at startup, loudly, if:

- `MARGIN_API_ACCOUNTS` is empty (nobody could log in);
- no account has a password hash and no OIDC verifier is registered (same);
- `MARGIN_API_STORAGE_ENCRYPTED=1` is not set.

The last one is a declaration, not a check. DuckDB has no at-rest encryption
of its own, so uploads, review history and the audit log are protected by the
volume or not at all, and the process cannot see the volume from inside. The
operator declares it, which makes it someone's decision rather than an
assumption.

### Connecting a real identity provider

`api.auth.set_oidc_verifier(fn)` takes a callable that validates the
provider's token and returns `(user_id, display_name, roles)`. Register it
before `create_app`, and bearer tokens are resolved through the provider
instead of local accounts. The engine deliberately ships no JWKS fetching or
signature validation of its own: that belongs to a library your security team
has chosen, and a half-written one would be worse than none. A provider
failure surfaces as 401, never 500, and never with the provider's message.

### What is logged, and what is not

`MARGIN_API_AUDIT_LOG` (default `.api_state/audit.log`) is JSON lines:
timestamp, immutable user id, action, status, resource. Logins, failed
logins, uploads, deletions and exports are recorded.

Request bodies, passwords, tokens, Authorization headers, uploaded content
and query strings are **not**, and the log refuses a field whose name looks
like a secret rather than trusting the caller. A test writes through the
whole flow and fails on any password, token or uploaded value found in the
file.

### Uploads

Uploads carry the id of whoever sent them, and that - not knowing the upload
id - is what grants access. The id is a content hash: two people uploading
the same export get the same id, and an id can be derived from a file someone
already has, so it was never a secret.

`MARGIN_API_UPLOADS_RETENTION_DAYS` (default 7) deletes uploads nobody came
back for; `DELETE /api/load/upload/{id}` removes one now. Files and folders
are created owner-only where the platform supports it - which on Windows it
does not, so the state directory's protection is the operator's to arrange.

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
