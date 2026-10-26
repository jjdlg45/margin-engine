/** Cost / tariff scenarios: the engine's precomputed pass-through grid with a
 * slider that snaps to grid points (one per 1% of pass-through). The browser
 * interpolates nothing - every displayed number is an exported grid point. */

import * as Slider from "@radix-ui/react-slider";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { QuerySection } from "../components/Async";
import { EntityCombobox } from "../components/filters/Combobox";
import { EmptyState, PageHeader, Section } from "../components/Page";
import type { ScenarioGrid } from "../types/bundle";
import { usePageData } from "../lib/queries";
import { count, dollars, pct } from "../lib/format";

function ScenariosBody({ grids }: { grids: ScenarioGrid[] }) {
  const [params, setParams] = useSearchParams();
  const name = params.get("scenario") ?? grids[0]?.name ?? "";
  const grid = useMemo(() => grids.find((g) => g.name === name) ?? grids[0], [grids, name]);
  // Index into the 0..100% grid; start at the 50% point whatever the grid's
  // resolution (the engine exports one point per 1% of pass-through).
  const [step, setStep] = useState(() =>
    grids.length ? Math.round((grids[0].points.length - 1) / 2) : 0,
  );
  if (!grids.length) {
    return (
      <>
        <PageHeader
          title="Cost / tariff scenarios"
          lede="Pass-through economics for a landed-cost shock, from the engine's precomputed grid."
        />
        <EmptyState title="No scenario grids in this run">
          The run produced no baseline pairs for any category or supplier
          shock, so there is nothing to slide through. Scenario grids appear
          when the data carries enough baseline volume in a scoped group.
        </EmptyState>
      </>
    );
  }
  const point = grid.points[step] ?? grid.points[0];
  return (
    <>
      <PageHeader
        title="Cost / tariff scenarios"
        lede={
          <>
            A +{pct(grid.pct_increase, 0)} landed-cost shock on one scope,
            passed through to prices at any level from 0 to 100% in 1% steps
            - every point precomputed by the engine; the slider snaps between
            them and full pass-through is never assumed. Contract-protected
            lines are excluded from pass-through and shown as protected
            exposure.
          </>
        }
      />
      <EntityCombobox
        label="Scenario"
        options={grids.map((g) => ({
          id: g.name,
          name: g.scope_label ?? g.scope_value,
          group: g.scope === "category" ? "Category" : "Supplier",
          hint: `${count(g.n_pairs)} pairs`,
        }))}
        value={grid.name}
        onChange={(v) =>
          setParams((p) => {
            if (v) p.set("scenario", v);
            else p.delete("scenario");
            return p;
          })
        }
      />
      <p className="mb-0 mt-3 max-w-2xl" data-testid="selected-scenario">
        Selected: a +{pct(grid.pct_increase, 0)} landed-cost increase on{" "}
        <span className="font-medium">{grid.scope_label ?? grid.scope_value}</span>{" "}
        ({grid.scope === "category" ? "category" : "supplier"} {grid.scope_value}), covering{" "}
        {count(grid.n_pairs)} customer-item pairs.
      </p>
      <div className="panel mt-4 max-w-2xl px-5 py-6 sm:px-8">
        <label className="text-caption secondary" htmlFor="pt-slider">
          Pass-through: <span className="font-semibold text-ink">{pct(point.pass_through, 0)}</span>{" "}
          of the cost increase priced through
        </label>
        <Slider.Root
          id="pt-slider"
          className="relative mt-3 flex h-5 w-full touch-none select-none items-center"
          min={0}
          max={grid.points.length - 1}
          step={1}
          value={[step]}
          onValueChange={([v]) => setStep(v)}
          aria-label="Pass-through level"
        >
          <Slider.Track className="relative h-1 grow rounded-full bg-ink/[0.12]">
            <Slider.Range className="absolute h-full rounded-full bg-petrol" />
          </Slider.Track>
          <Slider.Thumb aria-label="Pass-through level" className="block h-4 w-4 rounded-full border-2 border-petrol bg-card" />
        </Slider.Root>
        <dl className="m-0 mt-5 grid grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-3">
          <div>
            <dt className="text-caption secondary">Incremental cost</dt>
            <dd className="m-0 font-medium">{dollars(point.incremental_cost_cents)}</dd>
          </div>
          <div>
            <dt className="text-caption secondary">Recovered at this level</dt>
            <dd className="m-0 font-medium">{dollars(point.recovered_cents)}</dd>
          </div>
          <div>
            <dt className="text-caption secondary">Residual exposure</dt>
            <dd className="m-0 font-medium">{dollars(point.residual_exposure_cents)}</dd>
          </div>
          <div>
            <dt className="text-caption secondary">Protected (contract) exposure</dt>
            <dd className="m-0 font-medium">{dollars(point.protected_exposure_cents)}</dd>
          </div>
          <div>
            <dt className="text-caption secondary">Expected contribution effect</dt>
            <dd className="m-0 font-medium">
              {point.contribution_effect_cents !== null
                ? dollars(point.contribution_effect_cents)
                : "not supported"}
            </dd>
          </div>
        </dl>
        <p className="mb-0 mt-4 max-w-xl text-caption secondary">
          {point.contribution_effect_supported_everywhere
            ? "Every pair in scope has causal-supported elasticity, so the volume response behind the contribution effect is modeled for all of them."
            : point.contribution_effect_cents !== null
              ? "The contribution effect covers only pairs with causal-supported elasticity; pairs below that evidence level contribute no volume response here."
              : "No pair in scope reaches causal-supported elasticity, so the engine reports cost and recovery only - no volume response is modeled."}
        </p>
      </div>
      <Section title="COGS timing" lede={grid.cogs_timing}>
        <p className="m-0 max-w-2xl text-caption secondary">
          The timing statement comes from the declared cost basis of the
          mapped data and says when a landed-cost change reaches reported
          COGS under that method.
        </p>
      </Section>
    </>
  );
}

export function ScenariosPage() {
  const q = usePageData("scenarios");
  return (
    <div>
      <QuerySection query={q} label="cost and tariff scenarios" lines={8}>
        {(d) => <ScenariosBody grids={d.grids} />}
      </QuerySection>
    </div>
  );
}
