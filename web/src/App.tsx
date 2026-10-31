/** App shell: top navigation, persistent synthetic-data notice, theme toggle,
 * GitHub link, honest-labels footer. Detail routes beyond the Overview are
 * placeholders until the owner's design review (Run 3 hard stop). */

import * as Tooltip from "@radix-ui/react-tooltip";
import { HashRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { lazy, Suspense, useState } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { Boundary } from "./components/Async";
import { dataSource } from "./lib/api/client";
import { queryClient, useAppData } from "./lib/queries";
import { useCompact } from "./lib/useCompact";
import { Overview } from "./pages/Overview";

const Customers = lazy(() => import("./pages/Customers").then((m) => ({ default: m.Customers })));
const GuidancePage = lazy(() => import("./pages/GuidancePage").then((m) => ({ default: m.GuidancePage })));
const MethodologyPage = lazy(() => import("./pages/MethodologyPage").then((m) => ({ default: m.MethodologyPage })));
const ModulesPage = lazy(() => import("./pages/ModulesPage").then((m) => ({ default: m.ModulesPage })));
const OpportunityPage = lazy(() => import("./pages/OpportunityPage").then((m) => ({ default: m.OpportunityPage })));
const ScenariosPage = lazy(() => import("./pages/ScenariosPage").then((m) => ({ default: m.ScenariosPage })));
const SegmentsPage = lazy(() => import("./pages/SegmentsPage").then((m) => ({ default: m.SegmentsPage })));
const SensitivityPage = lazy(() => import("./pages/SensitivityPage").then((m) => ({ default: m.SensitivityPage })));
const WaterfallPage = lazy(() => import("./pages/WaterfallPage").then((m) => ({ default: m.WaterfallPage })));
const ActionCenterPage = lazy(() =>
  import("./pages/ActionCenterPage").then((m) => ({ default: m.ActionCenterPage })),
);
const ActionDetailPage = lazy(() =>
  import("./pages/ActionDetailPage").then((m) => ({ default: m.ActionDetailPage })),
);
const GuidePage = lazy(() => import("./pages/GuidePage").then((m) => ({ default: m.GuidePage })));
const RecommendedPage = lazy(() =>
  import("./pages/RecommendedPage").then((m) => ({ default: m.RecommendedPage })),
);
const LoadDataPage = lazy(() =>
  import("./pages/LoadDataPage").then((m) => ({ default: m.LoadDataPage })),
);

const PAGES = [
  { path: "/recommended", label: "Recommended actions", el: <RecommendedPage /> },
  { path: "/waterfall", label: "Waterfall", el: <WaterfallPage /> },
  { path: "/customers", label: "Customers & orders", el: <Customers /> },
  { path: "/segments", label: "Segments", el: <SegmentsPage /> },
  { path: "/guidance", label: "Guidance", el: <GuidancePage /> },
  { path: "/sensitivity", label: "Sensitivity", el: <SensitivityPage /> },
  { path: "/opportunity", label: "Opportunity", el: <OpportunityPage /> },
  { path: "/actions", label: "Action center", el: <ActionCenterPage /> },
  { path: "/scenarios", label: "Scenarios", el: <ScenariosPage /> },
  { path: "/modules", label: "Optional modules", el: <ModulesPage /> },
  { path: "/methodology", label: "Data & methodology", el: <MethodologyPage /> },
  { path: "/guide", label: "Use it with your own data", el: <GuidePage /> },
  // API/local mode only: the static demo has no server to upload to, so the
  // public site never lists or routes this page.
  ...(dataSource === "http"
    ? [{ path: "/load", label: "Load your data", el: <LoadDataPage /> }]
    : []),
];

/** Navigation grouped by the question a page answers, decision pages first.
 * Paths reference PAGES (the route table) so a page can never be reachable
 * but unlisted. The main groups hold what a pricing manager acts on; the
 * statistical machinery lives under Advanced analysis, present but second. */
const NAV_GROUPS: {
  label: string;
  advanced?: boolean;
  items: { path: string; label: string; end?: boolean }[];
}[] = [
  {
    label: "Margin",
    items: [
      { path: "/", label: "Overview", end: true },
      { path: "/customers", label: "Customers & orders" },
    ],
  },
  {
    label: "Pricing",
    items: [
      { path: "/recommended", label: "Recommended actions" },
      { path: "/guidance", label: "Price guidance" },
      { path: "/scenarios", label: "Scenarios" },
    ],
  },
  { label: "Actions", items: [{ path: "/actions", label: "Action center" }] },
  {
    label: "Data",
    items: [
      { path: "/methodology", label: "Data & methodology" },
      { path: "/guide", label: "Use it with your own data" },
      ...(dataSource === "http" ? [{ path: "/load", label: "Load your data" }] : []),
    ],
  },
  {
    label: "Advanced analysis",
    advanced: true,
    items: [
      { path: "/waterfall", label: "Waterfall" },
      { path: "/opportunity", label: "Opportunity levels" },
      { path: "/segments", label: "Segments" },
      { path: "/sensitivity", label: "Sensitivity" },
      { path: "/modules", label: "Optional modules" },
    ],
  },
];

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `whitespace-nowrap rounded-badge px-2 py-1 text-sm no-underline ${
    isActive ? "bg-petrol text-paper" : "text-ink/70 hover:text-ink"
  }`;

const sideLinkClass = ({ isActive }: { isActive: boolean }) =>
  `block rounded-badge px-2 py-1 text-sm no-underline ${
    isActive ? "bg-petrol text-paper" : "text-ink/70 hover:bg-inset hover:text-ink"
  }`;

function ThemeToggle() {
  const toggle = () => {
    const el = document.documentElement;
    const dark =
      el.dataset.theme === "dark" ||
      (!el.dataset.theme && window.matchMedia("(prefers-color-scheme: dark)").matches);
    el.dataset.theme = dark ? "light" : "dark";
  };
  return (
    <button
      onClick={toggle}
      className="rounded-badge border hairline px-2 py-1 text-caption secondary hover:text-ink"
      aria-label="Toggle light and dark theme"
    >
      Theme
    </button>
  );
}

/** Desktop navigation: a left sidebar. Decision pages in the main groups,
 * the statistical machinery under Advanced analysis after a divider. */
function Sidebar() {
  return (
    <nav aria-label="Primary" className="flex flex-col gap-5" data-testid="sidebar">
      {NAV_GROUPS.map((g) => (
        <div key={g.label} className={g.advanced ? "border-t hairline pt-4" : undefined}>
          <p className="m-0 mb-1 px-2 text-caption font-semibold uppercase tracking-wide secondary">
            {g.label}
          </p>
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
            {g.items.map((p) => (
              <li key={p.path}>
                <NavLink key={p.path} to={p.path} end={p.end} className={sideLinkClass}>
                  {p.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

/** Mobile navigation: a Menu button opening a grouped, collapsible list that
 * closes again on navigation. */
function MobileNav() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="rounded-badge border hairline px-2 py-1 text-caption secondary hover:text-ink"
        aria-expanded={open}
        aria-controls="mobile-nav"
        onClick={() => setOpen(!open)}
      >
        Menu
      </button>
      {open && (
        <nav id="mobile-nav" aria-label="Primary" className="order-last w-full pb-3">
          {NAV_GROUPS.map((g) => (
            <div key={g.label} className="mt-2 first:mt-0">
              <p className="m-0 mb-1 text-caption font-semibold secondary">{g.label}</p>
              <div className="flex flex-wrap gap-1">
                {g.items.map((p) => (
                  <NavLink
                    key={p.path}
                    to={p.path}
                    end={p.end}
                    className={navLinkClass}
                    onClick={() => setOpen(false)}
                  >
                    {p.label}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
      )}
    </>
  );
}

function Shell() {
  const app = useAppData();
  const compact = useCompact();
  return (
    <div className="mx-auto min-h-screen max-w-[1380px] px-4 sm:px-8">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-2 focus:z-20 focus:bg-card focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <header className="border-b hairline">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 pt-5">
          <Link to="/" className="text-ink no-underline">
            <span className="text-section font-semibold tracking-tight">Margin engine</span>
          </Link>
          <span className="ml-auto flex items-center gap-3">
            <a
              href="https://github.com/jjdlg45/margin-engine"
              className="text-caption text-petrol no-underline underline-offset-4 hover:underline"
            >
              Source on GitHub
            </a>
            <ThemeToggle />
          </span>
          {compact && <MobileNav />}
        </div>
        <p className="mb-2 mt-1 text-caption secondary" data-testid="data-notice">
          {app.data?.data_label ??
            "Synthetic demo data with planted ground truth"}{" "}
          - every figure reconciles to source and carries its evidence status.
          {app.data?.data_as_of && (
            <span data-testid="data-as-of"> Data as of {app.data.data_as_of}.</span>
          )}
          {Object.entries(app.data?.refresh_status ?? {})
            .filter(([, s]) => s.stale)
            .map(([name, s]) => (
              <span key={name} className="text-garnet" data-testid={`stale-${name}`}>
                {" "}
                {name.toUpperCase()} data is stale (last refresh {s.refreshed_utc}).
              </span>
            ))}
          {(app.data?.drift_flags ?? []).map((flag) => (
            <span key={flag.code} className="text-garnet" data-testid="drift-flag">
              {" "}
              {flag.message}
            </span>
          ))}
        </p>
      </header>
      <div className={compact ? undefined : "flex items-start gap-8"}>
        {!compact && (
          <aside className="sticky top-0 w-52 shrink-0 self-start overflow-y-auto pb-8 pt-5">
            <Sidebar />
          </aside>
        )}
        <main id="main" className="min-h-[85vh] min-w-0 flex-1">
        {app.isError && (
          <p className="py-16 text-garnet" role="alert">
            The app data could not be loaded ({app.error.message}). Run
            `margin-engine export-web` to generate the files under
            web/public/data/.
          </p>
        )}
        {!app.isError && (
          <Suspense fallback={<p className="py-16 secondary">Loading page…</p>}>
            <Routes>
              <Route
                path="/"
                element={
                  <Boundary label="the overview">
                    <Overview />
                  </Boundary>
                }
              />
              {PAGES.map((p) => (
                <Route
                  key={p.path}
                  path={p.path}
                  element={<Boundary label={p.label.toLowerCase()}>{p.el}</Boundary>}
                />
              ))}
              <Route
                path="/actions/:actionId"
                element={
                  <Boundary label="the action detail">
                    <ActionDetailPage />
                  </Boundary>
                }
              />
            </Routes>
          </Suspense>
        )}
        </main>
      </div>
      <footer className="border-t hairline py-6 text-caption secondary">
        Historical benchmarks are not optimal prices. Descriptive elasticities
        are not causal. Allocated amounts are not actuals.
      </footer>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider>
        <HashRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Shell />
        </HashRouter>
      </Tooltip.Provider>
    </QueryClientProvider>
  );
}
