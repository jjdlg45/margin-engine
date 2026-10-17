/** The signature visual: a custom-styled Recharts waterfall. Stage bars in
 * ink; the drops between stages hatched garnet with dollar + % of list labels.
 * One deliberate build animation on mount, skipped under reduced motion.
 * Values are differences of exported stage amounts — presentation arithmetic
 * only; no financial logic lives here. */

import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { WaterfallStage } from "../types/bundle";
import { dollars, dollarsShort, pct } from "../lib/format";

const DROP_NAMES = [
  "− Discounts",
  "− Rebates & credits",
  "− Product cost",
  "− Cost to serve",
];

interface Row {
  name: string;
  base: number;
  size: number;
  kind: "stage" | "drop-leak" | "drop-cost";
  coverage: string;
  pctOfList: number | null;
}

export function buildRows(stages: WaterfallStage[]): Row[] {
  const rows: Row[] = [];
  stages.forEach((s, i) => {
    rows.push({
      name: s.stage,
      base: 0,
      size: s.amount_cents,
      kind: "stage",
      coverage: s.coverage,
      pctOfList: s.pct_of_list ?? null,
    });
    const next = stages[i + 1];
    if (next) {
      rows.push({
        name: DROP_NAMES[i] ?? "Change",
        base: next.amount_cents,
        size: s.amount_cents - next.amount_cents,
        kind: i < 2 ? "drop-leak" : "drop-cost",
        coverage: next.coverage,
        pctOfList:
          s.pct_of_list !== null && next.pct_of_list !== null && s.pct_of_list !== undefined
            ? (s.pct_of_list ?? 0) - (next.pct_of_list ?? 0)
            : null,
      });
    }
  });
  return rows;
}

const reduced =
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function WaterfallTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: { payload: Row }[];
}) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="rounded-card border hairline bg-card px-3 py-2 text-caption shadow-lg">
      <p className="m-0 font-medium">{r.name}</p>
      <p className="m-0">{dollars(r.kind === "stage" ? r.size : -r.size)}</p>
      {r.pctOfList !== null && (
        <p className="m-0 secondary">
          {r.kind === "stage" ? `${pct(r.pctOfList)} of list` : `${pct(r.pctOfList)} of list value`}
        </p>
      )}
      <p className="m-0 secondary">{r.coverage}</p>
    </div>
  );
}

export function Waterfall({ stages, compact }: { stages: WaterfallStage[]; compact: boolean }) {
  const rows = buildRows(stages);
  const ink = "rgb(var(--ink))";
  const chart = (
    <BarChart
      data={rows}
      layout={compact ? "vertical" : "horizontal"}
      margin={compact ? { top: 8, right: 56, bottom: 4, left: 0 } : { top: 24, right: 8, bottom: 4, left: 8 }}
      barCategoryGap="22%"
    >
      <defs>
        {/* Leakage drops: garnet hatch. Cost drops: ink dots - costs are not
            leakage, so they get their own treatment. */}
        <pattern id="drop-hatch" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(135)">
          <rect width="6" height="6" fill="rgb(var(--garnet) / 0.16)" />
          <line x1="0" y1="0" x2="0" y2="6" stroke="rgb(var(--garnet) / 0.6)" strokeWidth="1.5" />
        </pattern>
        <pattern id="cost-dots" patternUnits="userSpaceOnUse" width="7" height="7">
          <rect width="7" height="7" fill="rgb(var(--ink) / 0.07)" />
          <circle cx="2" cy="2" r="1.2" fill="rgb(var(--ink) / 0.5)" />
        </pattern>
      </defs>
      {compact ? (
        <>
          <XAxis type="number" hide />
          <YAxis
            type="category"
            dataKey="name"
            width={118}
            tickLine={false}
            axisLine={false}
            tick={{ fill: "rgb(var(--ink) / 0.7)", fontSize: 11 }}
          />
        </>
      ) : (
        <>
          <XAxis
            dataKey="name"
            tickLine={false}
            axisLine={{ stroke: "rgb(var(--ink) / 0.12)" }}
            tick={{ fill: "rgb(var(--ink) / 0.7)", fontSize: 10.5 }}
            interval={0}
          />
          <YAxis hide />
        </>
      )}
      <Tooltip content={<WaterfallTooltip />} cursor={{ fill: "rgb(var(--ink) / 0.04)" }} />
      <Bar dataKey="base" stackId="w" fill="transparent" isAnimationActive={false} />
      <Bar
        dataKey="size"
        stackId="w"
        isAnimationActive={!reduced}
        animationDuration={600}
        animationBegin={100}
      >
        {rows.map((r) => (
          <Cell
            key={r.name}
            fill={
              r.kind === "stage"
                ? ink
                : r.kind === "drop-leak"
                  ? "url(#drop-hatch)"
                  : "url(#cost-dots)"
            }
          />
        ))}
        <LabelList
          dataKey="size"
          position={compact ? "right" : "top"}
          formatter={(v: number) => dollarsShort(v)}
          className="fill-current"
          style={{ fontSize: 11, fill: "rgb(var(--ink) / 0.87)" }}
        />
      </Bar>
    </BarChart>
  );
  return (
    <ResponsiveContainer width="100%" height={compact ? 380 : 300}>
      {chart}
    </ResponsiveContainer>
  );
}
