/** "Use it with your own data" (polish round): the practical path from a raw
 * ERP export to results, on your own machine. Static content - this hosted
 * demo never uploads anything; running on real data is a local workflow. */

import { Link } from "react-router-dom";
import { PageHeader, Section } from "../components/Page";

function Code({ children }: { children: string }) {
  return (
    <pre className="overflow-x-auto rounded-[8px] bg-inset/60 px-4 py-3 font-mono text-sm leading-relaxed">
      <code>{children}</code>
    </pre>
  );
}

export function GuidePage() {
  return (
    <div>
      <PageHeader
        title="Use it with your own data"
        lede="Everything on this site runs from synthetic demo data. Your own data never touches this site: you run the engine locally against your ERP export, and it refuses to guess anything it cannot verify."
      />

      <Section
        title="1. Install"
        lede="Two ways to run: Python directly (uv) or the containerized stack (Docker)."
      >
        <div className="max-w-2xl text-sm">
          <p className="mt-0">
            With <a href="https://docs.astral.sh/uv/">uv</a> installed, clone the repository and
            sync the environment:
          </p>
          <Code>{`git clone https://github.com/jjdlg45/margin-engine
cd margin-engine
uv sync`}</Code>
          <p>
            Or start the whole stack - engine export, API and UI - with Docker:
          </p>
          <Code>{`docker compose up --build
# UI:  http://localhost:8080
# API: http://localhost:8000/api/health`}</Code>
        </div>
      </Section>

      <Section
        title="2. Map your export"
        lede="The engine reads line-level invoice exports (CSV or .xlsx, one row per invoice line). A mapping file declares what every column means; nothing is guessed."
      >
        <div className="max-w-2xl text-sm">
          <p className="mt-0">
            The profiler scans your file and reports, per column: a type guess, the null rate,
            distinct counts, detected date formats, and candidate canonical fields - each with a
            confidence and a reason, plus the file-level delimiter, encoding and sign patterns:
          </p>
          <Code>{`uv run python -m margin_engine.cli profile your_export.csv`}</Code>
          <p>Then turn that into a draft mapping:</p>
          <Code>{`uv run python -m margin_engine.cli draft-mapping your_export.csv`}</Code>
          <p className="mb-0">
            Every guessed value in the draft is marked <span className="font-mono">CONFIRM</span>,
            and the engine refuses to run while any guess is unconfirmed - you review each one and
            delete its entry. Declare the semantics that matter: sign conventions, whether
            discounts are embedded, the cost basis and what it includes - or their honest absence
            (no cost means cost-dependent metrics say so instead of inventing numbers). Reference
            tables (price lists, rebate programs, display names) are optional; absent tables
            switch their features off and the report says so.
          </p>
        </div>
      </Section>

      <Section title="3. Run" lede="One command runs the pipeline and writes the report.">
        <div className="max-w-2xl text-sm">
          <Code>{`uv run python -m margin_engine.cli run \\
    --data your_export.csv \\
    --mapping config/mappings/your_mapping.yaml \\
    --out output`}</Code>
          <p className="mb-0">
            Open <span className="font-mono">output/report.html</span>; every table also lands as
            CSV and Parquet for Excel or Power BI. Reconciliation is exact to the cent or the run
            says exactly what did not tie. To browse results in this web app locally, add{" "}
            <span className="font-mono">export-web</span> and serve the{" "}
            <span className="font-mono">web/</span> app, or use the Docker stack above.
          </p>
        </div>
      </Section>

      <Section
        title="4. Tie out to the books"
        lede="Optional, recommended: prove the engine's totals against your general ledger before anyone reads the analysis."
      >
        <p className="m-0 max-w-2xl text-sm">
          Map a <span className="font-mono">gl_totals</span> table (month, net sales, cost of
          goods sold, optionally returns) and the report compares engine monthly totals to the
          books with absolute and percentage differences against a configurable tolerance.
          Failing months are flagged, never hidden, and the measure definitions are stated next
          to the comparison.
        </p>
      </Section>

      <Section
        title="5. Live mode"
        lede="A FastAPI service serves the same UI from a database, with scheduled refreshes and a replay feed for demos."
      >
        <div className="max-w-2xl text-sm">
          <Code>{`uv run python scripts/live_mode.py   # UI :4174 in API mode, API :8321`}</Code>
          <p className="mb-0">
            In live mode the shell shows "Data as of" per refresh, drift versus the previous run
            is flagged in plain language, and the Action center workflow runs against the API
            with the full review history stored in DuckDB.
          </p>
        </div>
      </Section>

      <Section
        title="6. Work the actions"
        lede="Findings become reviewable actions with guardrails; approval produces a price file your ERP imports."
      >
        <p className="m-0 max-w-2xl text-sm">
          The <Link to="/actions">Action center</Link> turns findings into a queue with owners,
          due dates and evidence. Analysts propose and submit; approvers approve or reject
          (never their own proposal); approving a compliance-flagged action requires a written
          justification; contract-protected lines are blocked outright. Approved price actions
          export as a CSV in your ERP's import format, and once implemented, the engine measures
          what actually happened against untreated comparison pairs. The process, including the
          steps outside the tool, is described on the Action center page.
        </p>
      </Section>

      <Section title="Where to read more">
        <ul className="m-0 max-w-2xl list-disc pl-5 text-sm">
          <li>
            <Link to="/methodology">Data &amp; methodology</Link> - reconciliation, coverage and
            validation for the data behind this demo.
          </li>
          <li>
            <a href="https://github.com/jjdlg45/margin-engine#running-on-real-data">
              README: Running on real data
            </a>{" "}
            - the same path with full flag documentation.
          </li>
          <li>
            <a href="https://github.com/jjdlg45/margin-engine/blob/main/docs/case-study-online-retail.md">
              Case study: Online Retail II
            </a>{" "}
            - the engine on a real public dataset with no cost and no list price.
          </li>
        </ul>
      </Section>
    </div>
  );
}
