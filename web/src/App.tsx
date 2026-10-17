/** App shell: top navigation, persistent synthetic-data notice, theme toggle,
 * GitHub link, honest-labels footer. Detail routes beyond the Overview are
 * placeholders until the owner's design review (Run 3 hard stop). */

import * as Tooltip from "@radix-ui/react-tooltip";
import { HashRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { BundleProvider, useBundleState } from "./lib/bundle";
import { Customers } from "./pages/Customers";
import { GuidancePage } from "./pages/GuidancePage";
import { MethodologyPage } from "./pages/MethodologyPage";
import { ModulesPage } from "./pages/ModulesPage";
import { OpportunityPage } from "./pages/OpportunityPage";
import { Overview } from "./pages/Overview";
import { ScenariosPage } from "./pages/ScenariosPage";
import { SegmentsPage } from "./pages/SegmentsPage";
import { SensitivityPage } from "./pages/SensitivityPage";
import { WaterfallPage } from "./pages/WaterfallPage";

const PAGES = [
  { path: "/waterfall", label: "Waterfall", el: <WaterfallPage /> },
  { path: "/customers", label: "Customers", el: <Customers /> },
  { path: "/segments", label: "Segments", el: <SegmentsPage /> },
  { path: "/guidance", label: "Guidance", el: <GuidancePage /> },
  { path: "/sensitivity", label: "Sensitivity", el: <SensitivityPage /> },
  { path: "/opportunity", label: "Opportunity", el: <OpportunityPage /> },
  { path: "/scenarios", label: "Scenarios", el: <ScenariosPage /> },
  { path: "/modules", label: "Optional modules", el: <ModulesPage /> },
  { path: "/methodology", label: "Data & methodology", el: <MethodologyPage /> },
];

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

function Shell() {
  const state = useBundleState();
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
          <nav aria-label="Primary" className="order-last -mx-1 flex w-full flex-wrap gap-1 pb-2 sm:order-none sm:w-auto sm:pb-0">
            <NavLink
              to="/"
              end
              className={({ isActive }) =>
                `whitespace-nowrap rounded-badge px-2 py-1 text-sm no-underline ${
                  isActive ? "bg-petrol text-paper" : "text-ink/70 hover:text-ink"
                }`
              }
            >
              Overview
            </NavLink>
            {PAGES.map((p) => (
              <NavLink
                key={p.path}
                to={p.path}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-badge px-2 py-1 text-sm no-underline ${
                    isActive ? "bg-petrol text-paper" : "text-ink/70 hover:text-ink"
                  }`
                }
              >
                {p.label}
              </NavLink>
            ))}
          </nav>
          <span className="ml-auto flex items-center gap-3">
            <a
              href="https://github.com/jjdlg45/margin-engine"
              className="text-caption text-petrol no-underline underline-offset-4 hover:underline"
            >
              Source on GitHub
            </a>
            <ThemeToggle />
          </span>
        </div>
        <p className="mb-2 mt-1 text-caption secondary">
          Synthetic demo data with planted ground truth - every figure
          reconciles to source and carries its evidence status.
        </p>
      </header>
      <main id="main">
        {state.status === "loading" && <p className="py-16 secondary">Loading engine results…</p>}
        {state.status === "error" && (
          <p className="py-16 text-garnet">
            The data bundle could not be loaded ({state.message}). Run
            `margin-engine export-web` to generate web/public/data/bundle.json.
          </p>
        )}
        {state.status === "ready" && (
          <Routes>
            <Route path="/" element={<Overview />} />
            {PAGES.map((p) => (
              <Route key={p.path} path={p.path} element={p.el} />
            ))}
          </Routes>
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
    <Tooltip.Provider>
      <BundleProvider>
        <HashRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Shell />
        </HashRouter>
      </BundleProvider>
    </Tooltip.Provider>
  );
}
