# Supply-chain security

How this repository keeps its dependencies honest, and what to do when an
audit finds something.

## What runs automatically

- **Dependabot** (`.github/dependabot.yml`) opens weekly update pull requests
  for the three ecosystems this repo ships: Python (uv lockfile, repo root),
  npm (`web/`), and the GitHub Actions used by the workflows. Minor and patch
  updates arrive grouped; majors arrive individually.
- **CI audits on every push and pull request** (`.github/workflows/ci.yml`):
  - `pip-audit` over the exact locked Python set (`uv export --frozen
    --all-groups`), failing on any known vulnerability;
  - `npm audit --audit-level=high` in `web/`, failing on high or critical
    advisories. Moderate advisories do not fail the build but are reviewed
    (see the accepted list below).

## How to handle a finding

1. **Read the advisory**, not just the severity: does the vulnerable code
   path exist in this project's use? (Example: a dev-server-only hole matters
   less on CI than in `npm run dev` on an open network - but fix it anyway
   when a compatible release exists.)
2. **Prefer the smallest compatible upgrade.** Python:
   `uv lock --upgrade-package <name>` then `uv sync --all-groups` and the full
   test suite. npm: install the fixed version within the current major when
   one exists; majors only with the full web gates (tsc, vitest, build, both
   Playwright suites - a browser bump can shift visual snapshots, which must
   be reviewed and re-baselined deliberately, never blindly).
3. **Re-run the audits locally** the same way CI does, and record the change
   in `docs/build-log.md` (what was vulnerable, what fixed it, what the tests
   said).
4. **If no fixed release exists**, decide explicitly: pin, patch, replace, or
   accept temporarily - and write the acceptance down here with its reason.
   Never silence an audit without a written entry.
5. **Never** disable an audit step to make CI green.

## Accepted advisories (reviewed, not silenced)

- **tailwindcss 3.x chain** (`postcss-selector-parser` quadratic parsing,
  moderate): the fix is Tailwind 4, a breaking build-pipeline migration. The
  parser runs only at build time on this repo's own stylesheets, never on
  user input. Revisit with the Tailwind 4 migration.
- **react-router v6 open-redirect variants** (moderate; fixed only in v7):
  this app uses hash routing on a static page with no same-origin redirect
  flows; the v6 line already carries the high-severity fixes (6.30.6).
  Revisit with a React Router 7 migration.

Both entries fail closed in the sense that CI's high-severity gate stays
active; any escalation of these advisories to high re-breaks the build.
