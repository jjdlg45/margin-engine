# Connecting Infor, SAP, Epicor, NetSuite, or a TMS

The engine's file-drop connector covers the way mid-size companies actually
connect large ERPs and transportation systems to outside tools today: the
source system exports files on a schedule to an agreed location, and the
tool picks them up. No ERP-side plugin, no vendor API contract, no write
access to the source system.

This guide is deliberately general. Export scheduling lives in each
product's own administration tools and varies by version and configuration;
the steps below describe the pattern without claiming product-specific
click-paths that were not verified against a live system.

## The pattern

1. **Pick the data.** The engine reads invoice lines plus optional reference
   tables (customers, items, price lists, rebate settlements, GL monthly
   totals, and so on). Most ERPs have a report writer, a scheduled query, or
   an integration/ETL module that can write any of these as CSV on a
   schedule. Export the same columns every time; the mapping file declares
   what each column means.
2. **Pick the location.** Either a folder the engine's machine can see (a
   local path or a network share) or an SFTP site. Key-based SFTP is the
   common choice when the ERP is hosted.
3. **Schedule the export.** Daily after close of business is typical. Have
   the export write to a temporary name and rename when finished, or write a
   `<name>.done` marker file afterward - the connector only picks up
   complete files (a marker, or a size that is stable across a settle
   interval).
4. **Describe the files to the engine.** One pick-up rule per file kind in
   `config/filedrop.yaml` (filename pattern, mapping file, role), and a
   mapping YAML that declares each column's business meaning. The
   `margin-engine profile` and `draft-mapping` commands build the mapping
   from a sample export; a person confirms every guessed field.
5. **Run it.** `margin-engine connect filedrop --check` verifies the
   location is reachable; `margin-engine connect filedrop` picks up, validates
   and materializes the files; in API mode the scheduler does this on an
   interval and the Connections screen shows status, counts and warnings.

## What the connector guarantees

- Only complete files are picked up; a file still being written waits for
  the next sync.
- Every accepted file's checksum is recorded; identical bytes are never
  processed twice, even under a new filename.
- Accepted files are archived (watched folder: into `archive/` next to the
  drop; SFTP: the remote file is never touched - the engine is read-only
  toward the source - and the downloaded copy is archived locally).
- A file that matches no rule, fails to parse under its mapping, or lacks a
  required column is quarantined with the reason written next to it in
  plain English. Nothing is silently skipped or silently guessed.
- Each sync writes a manifest (window, counts, checksums, warnings) that
  the engine's run manifest links to, so every result names the sync that
  fed it.

## Product notes (general, not verified click-paths)

- **Infor CloudSuite / M3 / LN:** scheduled exports are usually set up
  through the product's integration or ION tooling, or a scheduled report
  writing to a file location the engine can reach.
- **SAP (ECC / S/4HANA):** scheduled background jobs or CDS/ABAP query
  extracts commonly write CSV to an application server path or SFTP; many
  sites already have such extracts for BI tools and can add one more
  recipient.
- **Epicor Kinetic:** BAQ (Business Activity Query) exports can be scheduled
  to write files; sites often schedule them nightly.
- **NetSuite:** saved searches can be scheduled and delivered to SFTP; the
  CSV columns follow the saved search definition.
- **TMS products:** most support scheduled freight/shipment extracts to SFTP
  as standard carrier/EDI-adjacent functionality.

If your system can call REST APIs instead, the QuickBooks Online and HubSpot
connectors show the API pattern; `docs/connectors.md` describes how to add a
connector against the same interface.
