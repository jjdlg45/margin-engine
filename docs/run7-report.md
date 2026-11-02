# Run 7 report — System connectors

Branch `run7`, built 2026-11-01/02. Goal: pull data from company systems on
a schedule instead of manual exports, with every connector producing the
same canonical files the engine already ingests, so nothing downstream
changed. One commit per phase; full check results per phase in
`docs/build-log.md`.

## What was built

### Phase 90 — Connector framework

- `SystemConnector` interface (`describe` / `check` / `sync(since)`), with
  materialization owned by the framework (`run_sync`): canonical CSVs plus a
  sync manifest (connector name and version, source, window, record counts
  per object, checksum per file, warnings, cursor) into a per-sync folder
  under `.api_state/connectors/<name>/syncs/<id>/`.
- Incremental sync with a locally stored cursor; re-running with no changes
  keeps the previous folder (content-hash idempotency); full resync
  available; late edits handled by per-connector re-fetch windows plus
  upsert-by-key; tombstones delete by key or by any column subset (one
  tombstone removes every line of a deleted invoice).
- Staging-then-swap everywhere: a partial failure leaves the previous good
  sync, the state and the cursor untouched.
- The run manifest links `sync_manifest.json` when an input folder came from
  a connector, so every engine run names the sync that fed it.
- Credentials: environment / `.env` resolution, plus a Fernet
  encrypted-at-rest keystore for production mode (`MARGIN_CONNECTOR_KEY`);
  secret values are scrubbed from every output and a connector that tries to
  write one into its manifest aborts.
- Scheduler: `MARGIN_API_CONNECTORS="name[:interval_s],..."` adds one job
  per connector to the live-mode scheduler; manual run-now shares the same
  code path and a per-connector lock (second caller gets a plain refusal).
- Tests (15): the fake-connector contract — manifest completeness, cursor
  advance, idempotent re-run, partial-failure rollback, tombstones, full
  resync, run-manifest linking, credential resolution order, keystore
  round-trip with no plaintext at rest, no secret in any output file.

### Phase 91 — File-drop connector (verified end to end)

- Sources: a local/network watched folder, and SFTP with key-based auth
  only; unknown host keys are rejected (known_hosts required), and the
  connector is strictly read-only toward the server — the remote file is
  never moved; the downloaded copy is archived locally and the checksum
  ledger prevents reprocessing.
- Pick-up rules (`config/filedrop.yaml`): filename pattern -> mapping +
  role. Only complete files are picked up (`.done` marker or stable size
  across a settle interval); accepted files are validated against their
  mapping (parse under declared input options + required columns) before
  acceptance and served verbatim (byte-identical) under the filename the
  mapping expects; failures are quarantined with the reason written next to
  the file; identical bytes are never processed twice.
- CLI: `margin-engine connectors` and `margin-engine connect <name>
  [--check|--full-resync|--authorize]`.
- Guide: `docs/connecting-erp-file-drop.md` — the general pattern for Infor,
  SAP, Epicor, NetSuite and TMSs, explicitly without unverified
  product-specific click-paths.
- Tests (15): watched-folder end to end including an engine ingest of the
  materialized folder and byte-identical pass-through; incomplete-file wait;
  duplicate never processed twice; wrong-format and no-rule quarantine with
  reasons; permission errors (file left for the next sync); an in-process
  paramiko SFTP server (`tests/sftp_server.py`) proving the SFTP path end to
  end, read-only behaviour, remote permission-denied, host-key rejection;
  rules validation errors.

### Phase 92 — QuickBooks Online (built against the documented API)

- OAuth 2.0 authorization-code flow with rotated refresh tokens persisted to
  a local token cache; one-time consent via `--authorize` (localhost
  listener) or Intuit's OAuth playground. Minor version pinned to 75 (the
  only supported one since 2025-08-01); rate limits per Intuit's
  documentation (500/min per realm, 429 -> 60 s wait) — sources and terms
  recorded in `docs/data-sources.md`.
- Pulls customers, items, invoices + lines, credit memos (CDC incremental
  with tombstones; full queries with pagination otherwise) and the monthly
  ProfitAndLoss report into `gl_totals.csv` for the GL variance check.
  Committed mapping: `config/mappings/quickbooks_online.yaml`.
- Honest data gaps, stated in the mapping and the connector: QuickBooks
  lines carry no unit cost (item purchase cost where present, otherwise
  blank -> `cost_unavailable`, never invented; a current purchase cost is
  not a transaction-time cost); credit-memo lines are stored positive, so
  the connector writes the revenue-effect sign and the mapping documents it.
- Money is parsed `parse_float=Decimal`; no float ever touches an amount.
- Tests (11 offline, documented-schema fixtures labeled as such): full sync
  with an engine ingest to the cent (sale 10500 cents, credit −2100 cents,
  cost joined at scale, service line cost absent), CDC upserts + deleted
  documents removing their lines, re-fetch window applied, pagination,
  token rotation persisted and never present in any output, 401 refresh
  retry, 429 backoff, P&L parser edge cases, environment validation. One
  opt-in `live` test (full sandbox sync + engine run) skips without
  credentials.

### Phase 93 — HubSpot CRM (built against the documented API)

- Private-app token, read-only scopes (companies, deals, line items - and
  only those: HubSpot's separate Quotes object is not read in this run, so
  its scope is not declared either). Pulls companies (customer names) and
  deals with line items; stage won/lost comes from each pipeline's own
  metadata (`isClosed`, `probability`), never from stage-name guessing.
- Materializes `customers.csv` and `quotes.csv` where only closed-won /
  closed-lost deals carry outcomes; open, stage-unknown, company-less and
  line-item-less deals are excluded with per-deal reasons in
  `quote_exclusions.csv` (consistent with Phase 77), and exclusions clear
  when a deal is later decided. Incremental sync via the search endpoint;
  archived deals/companies arrive as tombstones. HubSpot line items carry
  one price, so no close price is invented and the negotiation-gap analysis
  stays off.
- Tests (5 offline + 1 live opt-in): full sync shapes and reasons,
  incremental + archival tombstones, Retry-After honored on 429, check
  messages, and a merge test proving the materialized quotes table loads
  through a main mapping's `quotes` reference-table declaration.

### Phase 94 — Connections screen

- API: `GET /api/connections`, `POST /api/connections/{name}/check`,
  `POST /api/connections/{name}/sync`, `PUT /api/connections/{name}/schedule`
  (persisted overrides that re-arm the scheduler task). Admin-only in
  production mode (new `admin` role extends Phase 82; client-supplied
  identity stays rejected there); open in local single-user mode. OpenAPI
  updated; 8 contract/parity/role tests.
- UI: a Connections page in the Data group. Static demo: an explanatory
  page from the bundle (what each connector does, how to set it up, no live
  controls, no credentials). API/local mode: per-connector status
  (configured / missing settings with purposes), last sync time and record
  counts, warnings and failures in plain English, Check connection, Sync
  now (idempotency surfaced as "no changes since the last one"), and a
  schedule editor. Loading skeletons, error + retry, axe clean, label
  scanner clean, snapshot baselines recorded (light/dark × desktop/mobile).
- e2e: static suite additions (nav, labels, quality, livedata size gate) and
  an API-mode spec driving a real file-drop sync through the UI.

### Phase 95 — Documentation

- README "Connecting your systems"; `docs/connectors.md` (interface, rules,
  how to add one, contract tests); `docs/data-sources.md` entries for Intuit
  and HubSpot (endpoints, terms, limits, verification dates); this report.

## Two things self-review caught before the final commit

- **A scope the code never used.** HubSpot's declared scopes included
  `crm.objects.quotes.read` while the connector reads only companies, deals
  and line items. Dropped, with the reason recorded in the module docstring,
  the README table and `docs/connectors.md`: the quote rows come from decided
  deals, and a declared-but-unused scope misdescribes what the token is for.
- **A demo card that mirrored a developer's machine.** The static
  Connections page built its file-drop card from the connector's own
  `describe()`, which reports the *local* pick-up rules, via a CWD-relative
  config path - so the hosted bundle's content depended on where
  `export-web` ran (empty file list from anywhere but the repo root). The
  static card now names the committed example from a constant, with a test
  asserting the section is identical when built from two different working
  directories. The existing cross-process determinism test could not see
  this, because it runs both processes from the repo root.

## Three pre-existing failures surfaced by the Run 7 gates (fixed)

All three reproduced on unmodified `main` and are not Run 7 regressions;
each got a regression test.

1. **The demo review workflow had died by calendar.** Phase 69's expiry
   compared `expires_on` (data-anchored: demo world ends 2026-01-28 + 90
   days = 2026-04-28) against the wall clock, so from 2026-04-28 every
   seeded demo action was permanently un-approvable (`stale_action`), and
   the API-mode lifecycle e2e failed. Fix: expiry is judged against the
   engine's own data date — the newest engine-authored event in the store
   (`evidence_as_of`), which regeneration advances — falling back to the
   wall clock. Identical behaviour in live deployments; a frozen demo world
   no longer goes stale by the calendar alone. Same clock used at approval
   and export. (This matches the existing convention: overdue is already
   computed from event dates.)
2. **Every manual proposal 500'd.** `ActionStore.add_action` inserted
   positionally (`INSERT INTO actions SELECT *`); the ux round added action
   columns, so the route's column order no longer matched a store seeded
   from parquet and a scope-key string was cast into an INT64 column. Fix:
   `INSERT ... BY NAME` for actions, events and edits, plus a regression
   test that seeds a store with reversed column order.
3. **Load-data e2e locator drift.** The mapping-column option's accessible
   name gained a dtype hint ("INV_DT date"), so the exact-name locator never
   matched; the spec now matches the option by its exact column-name text.

## Verification summary

- Python: ruff, ruff format, mypy (strict, 103 files), full pytest
  (per-phase results in the build log; final full-suite run before the last
  commit).
- Web: tsc, vitest, production build, static Playwright suite (axe, label
  scanner, nav, size gates, snapshots incl. 4 new baselines), API-mode
  Playwright suite 15/15 against the real BFF.
- Fresh-clone verification before the final commit (build log).

## Waiting on the owner

Live verification of the two API connectors needs free developer resources
only the owner can create (nothing else in Run 7 depends on them):

1. **QuickBooks:** an Intuit developer account + app (accounting scope) and
   its sandbox company. Put in `.env`: `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`,
   `QBO_REALM_ID`, and either run
   `uv run margin-engine connect quickbooks_online --authorize` once
   (redirect URI `http://localhost:8723/callback` registered on the app) or
   set `QBO_REFRESH_TOKEN` from Intuit's OAuth playground.
2. **HubSpot:** a free developer test account + a private app with read-only
   scopes for companies, deals, line items and quotes. Put `HUBSPOT_TOKEN`
   in `.env`.
3. Then: `uv run margin-engine connect <name> --check`, a sync, and the
   opt-in live tests (`uv run python -m pytest -m live`). After a successful
   end-to-end run, update each connector's `live_verification` text, the
   README table and `docs/data-sources.md` with the date, and replace the
   documented-schema fixtures with recorded sandbox responses.

Never paste credentials into chat or commit them; `.env` is gitignored.

## Known gaps

- QuickBooks Online and HubSpot are **not verified live** (above); their
  fixtures encode the documented schemas, not recorded responses, and are
  labeled as such in the test files.
- USITC HTS (Phase 29.3 KNOWN GAP) is unchanged by this run.
- The `always_positive_with_type` sign convention is declared in the mapping
  schema but not applied anywhere in the pipeline (observed while building
  the QuickBooks mapping; the connector writes revenue-effect signs instead
  of relying on it). Logged as an observation, not changed in this run.
