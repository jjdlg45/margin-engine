/** App shell: top navigation, persistent synthetic-data notice, theme toggle,
 * GitHub link, honest-labels footer. Detail routes beyond the Overview are
 * placeholders until the owner's design review (Run 3 hard stop). */

import * as Tooltip from "@radix-ui/react-tooltip";
import { HashRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { lazy, Suspense, useState } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { Boundary } from "./components/Async";
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

const PAGES = [
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
];

/** Navigation grouped by the question a page answers. Paths reference PAGES
 * (the route table) so a page can never be reachable but unlisted. */
const NAV_GROUPS: { label: string; items: { path: string; label: string; end?: boolean }[] }[] = [
  {
    label: "Margin",
    items: [
      { path: "/", label: "Overview", end: true },
      { path: "/waterfall", label: "Waterfall" },
      { path: "/customers", label: "Customers & orders" },
    ],
  },
  {
    label: "Pricing",
    items: [
      { path: "/segments", label: "Segments" },
      { path: "/guidance", label: "Guidance" },
      { path: "/sensitivity", label: "Sensitivity" },
      { path: "/opportunity", label: "Opportunity" },
      { path: "/scenarios", label: "Scenarios" },
    ],
  },
  { label: "Actions", items: [{ path: "/actions", label: "Action center" }] },
  {
    label: "Data",
    items: [
      { path: "/methodology", label: "Data & methodology" },
      { path: "/modules", label: "Optional modules" },
      { path: "/guide", label: "Use it with your own data" },
    ],
  },
];

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `whitespace-nowrap rounded-badge px-2 py-1 text-sm no-underline ${
    isActive ? "bg-petrol text-paper" : "text-ink/70 hover:text-ink"
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

/** Desktop navigation: four labeled groups in one row (wrapping to a second
 * line where needed), separated by hairlines. */
function DesktopNav() {
  return (
    <nav
      aria-label="Primary"
      className="order-last -mx-1 flex w-full flex-wrap items-center gap-y-1 pb-2"
    >
      {NAV_GROUPS.map((g, i) => (
        <span key={g.label} className="flex items-center">
          {i > 0 && <span aria-hidden="true" className="mx-2 h-4 w-px bg-ink/[0.12]" />}
          <span className="mr-1 px-1 text-caption secondary">{g.label}</span>
          {g.items.map((p) => (
            <NavLink key={p.path} to={p.path} end={p.end} className={navLinkClass}>
              {p.label}
            </NavLink>
          ))}
        </span>
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
    <div className="mx-auto min-h-screen max-w-[1120px] px-4 sm:px-8">
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
          {compact ? <MobileNav /> : <DesktopNav />}
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
      <main id="main" className="min-h-[85vh]">
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
