# System connectors (Run 7)

A system connector pulls records from a company system - an ERP, an
accounting system, a CRM, or files it drops on a schedule - and materializes
them as the **same canonical CSV files the engine already ingests through a
mapping**. Nothing downstream changes: mapping, validation, reconciliation
and every test read a connector's folder exactly as they read a manual
export.

Built-in connectors:

| Connector | System | Verification status |
|---|---|---|
| `filedrop` | Watched folder / SFTP (how Infor, SAP, Epicor, NetSuite and TMSs connect in practice; see docs/connecting-erp-file-drop.md) | Verified end to end against a local watched folder and an in-process SFTP server |
| `quickbooks_online` | QuickBooks Online (Intuit Accounting API, minorversion 75) | Built against the documented API, not yet verified live (needs the owner's sandbox) |
| `hubspot` | HubSpot CRM v3 (companies, deals, line items) | Built against the documented API, not yet verified live (needs the owner's test account) |

Endpoints, licenses/terms and rate limits for the API connectors are
recorded in `docs/data-sources.md`. A connector is only ever described as
working after an end-to-end run against the real service or its official
sandbox; anything verified only against documented schemas says so in its
`live_verification` text, on the Connections screen, and in the README.

## Using connectors

```
margin-engine connectors                         # list them, with status
margin-engine connect <name> --check             # credentials + reachability
margin-engine connect <name>                     # sync now
margin-engine connect <name> --full-resync       # ignore the cursor
margin-engine connect quickbooks_online --authorize   # one-time OAuth consent
```

A sync materializes canonical files plus `sync_manifest.json` under
`.api_state/connectors/<name>/syncs/<sync-id>/`. Run the engine on that
folder (`--data` pointing at it); the run manifest links the sync manifest,
so every result names exactly which sync fed it.

In API mode, `MARGIN_API_CONNECTORS="filedrop:900,quickbooks_online:86400"`
schedules syncs (reusing the live-mode scheduler), and the Connections
screen shows status, last sync, counts and warnings with "Check connection",
"Sync now" and schedule settings. In production mode those controls require
the `admin` role; the static GitHub Pages demo shows an explanatory page
with no live controls.

Credentials come from the environment or a local `.env` (gitignored), never
the repo, logs, outputs or manifests. In production mode an encrypted
keystore (`margin_engine.connectors.credentials.EncryptedKeyStore`, Fernet,
key via `MARGIN_CONNECTOR_KEY`) holds server-side credentials encrypted at
rest.

## The interface

A connector implements `SystemConnector`
(`src/margin_engine/connectors/framework.py`):

- `describe()` -> `ConnectorInfo`: name, version, system, what it produces,
  required settings (each marked secret/optional), scopes, and the honest
  `live_verification` text.
- `check()` -> `CheckResult`: credentials and reachability, in plain English.
- `sync(since)` -> `SyncBatch`: records new or changed since the cursor
  (`None` = everything), as either
  - `tables`: per-table upserts keyed by declared key columns, plus optional
    tombstones (`deletes` may carry any column subset - e.g. one parent
    document column removes every line of a deleted invoice), or
  - `raw_files`: validated source files served verbatim (the file-drop
    path), carried forward until replaced.

The framework (`run_sync`) owns everything else: merging upserts into local
state, materializing the per-sync folder, checksums, the sync manifest, the
cursor, idempotent re-runs (identical content keeps the previous folder),
and staging-then-swap so a partial failure leaves the previous good sync in
place. Connectors apply their own re-fetch window to `since` so
late-arriving edits are picked up again; upserting by key makes that
idempotent.

Rules every connector follows:

- **Read-only.** The narrowest read-only scopes available; never write to
  the source system (the SFTP source is never renamed or deleted; archival
  happens on the local copy).
- **No credential anywhere.** Secret setting values are scrubbed from
  warnings and manifests; a connector that tries to write one into its
  manifest aborts the sync. Tests prove no secret reaches any output file.
- **Plain-English failures.** Every error says what happened and what to do;
  never a raw stack trace or a silent guess.
- **Decimal money.** API transports parse JSON numbers with
  `parse_float=Decimal`; money never passes through a float.

## Adding a connector

1. Implement `SystemConnector` in `src/margin_engine/connectors/<name>.py`
   and register it: `register("<name>", lambda creds: MyConnector(creds))`.
2. Take an injectable transport (see `quickbooks.py`), so offline tests run
   against recorded or documented-schema fixtures; put live calls only in an
   opt-in `@pytest.mark.live` test that skips cleanly without credentials.
3. Write the canonical columns a mapping can declare, and commit the mapping
   (or document the reference-table block, as the HubSpot connector does).
4. Verify each external source's current endpoint, terms and rate limits
   before first use and record them in `docs/data-sources.md`; respect the
   limits with a client-side limiter and backoff.
5. Prove the contract with the framework tests as a template
   (`tests/test_connector_framework.py`): idempotency, cursor advance,
   partial-failure rollback, tombstones, manifest completeness, no secrets
   in any output - plus an engine ingest of the materialized folder.
6. Label verification honestly: `live_verification` says "built against the
   documented API, not yet verified live" until a real end-to-end run
   happened, and names the date when it did.

Salesforce note: a Salesforce connector would follow the HubSpot pattern
exactly (free Developer Edition, REST query API, read-only scopes); it is
documented here rather than built.
