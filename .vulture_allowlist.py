"""Names vulture must not report, each with the reason it is unreachable.

Run 8. The dead-code gate scans src/, api/, synth/ and scripts/ and NOT
tests/, on purpose: the wiring audit's whole subject was code that tests kept
looking alive. Scanning tests would hide exactly the class of thing this gate
exists to catch.

Framework-registered code is handled by --ignore-decorators in the Makefile /
CI invocation (FastAPI routes and exception handlers, Typer commands, pydantic
validators), not here. This file is for names no decorator explains.

Adding a name here is a decision, not a formality. It needs a reason that
survives being read aloud, and the companion test in
tests/test_docs_match_code.py independently checks that anything the DOCS
describe as active really does have a call site.
"""

# --- stdlib hooks: the signature and name belong to the base class ----------

# http.server.BaseHTTPRequestHandler, subclassed for the one-shot OAuth
# callback listener in connectors/quickbooks.py. The stdlib instantiates the
# handler and calls these; log_message is overridden with an empty body to
# keep the access log off the console, and its (fmt, *args) signature is fixed
# by the stdlib's own log_request call.
do_GET  # noqa: B018, F821
log_message  # noqa: B018, F821

# --- deliberately unwired, and disclosed as such in the docs ---------------
# These four are the Run 8 audit's keep-with-reason decisions. The docs say
# "not wired in" where a reader will see it, and test_docs_match_code.py fails
# if that wording disappears or if any of them gains a caller.

optimize_targets  # noqa: B018, F821
optimal_price_win_aware  # noqa: B018, F821
run_cost_scenario  # noqa: B018, F821
fetch_tariff_rates  # noqa: B018, F821

# --- read by tests only, by design ------------------------------------------
# Query and injection surfaces that exist so a test can assert on state
# without reaching into internals. Each one is the alternative to a test that
# would break on every refactor. Listed individually rather than waved
# through, so the list stays short enough to argue with.

# Lets a test install a fake OIDC verifier; production injects the real one.
set_oidc_verifier  # noqa: B018, F821
# Reads the README's generated validation-table block for the drift test that
# makes "no number here is hand-typed" true.
current_block  # noqa: B018, F821
# Returns the newest materialized sync folder, used by the connector
# framework's idempotency and swap assertions.
latest_dir  # noqa: B018, F821
# The Phase 69 revalidation audit trail, queried by the stale-action tests.
revalidations  # noqa: B018, F821
# Whether a market-control source is absent; read by the gate tests.
absent  # noqa: B018, F821
