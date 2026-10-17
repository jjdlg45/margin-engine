/** The signature visual: a custom-styled Recharts waterfall.
 *
 * Visual grammar (docs/ui/design-plan.md): solid ink = totals, garnet hatch =
 * pricing leakage, ink dots = costs. Floating deduction bars sit on a faint
 * ghost column down to the axis so they read as resting on the remaining
 * amount, and thin dashed connectors trace the step-down flow. Every bar and
 * connector meets at least 3:1 contrast against the panel in both themes.
 * One deliberate build animation on mount, skipped under reduced motion.
 * Values are differences of exported stage amounts — presentation arithmetic
 * only; no financial logic lives here. */

import {
  Bar,
  BarChart,
  Cell,
  Customized,
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

interface BarRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Dashed step connectors drawn from each bar to the next, at the level where
 * the next bar starts — the classic waterfall reading aid. Positions come
 * from the rendered bar rects, so they stay exact under any scale. */
function Connectors(props: Record<string, unknown> & { verticalLayout?: boolean }) {
  const items = props.formattedGraphicalItems as
    | { props: { dataKey?: string; data?: (BarRect | null)[] } }[]
    | undefined;
  const withData = (items ?? []).filter(
    (it) => Array.isArray(it.props?.data) && (it.props.data?.length ?? 0) > 0,
  );
  const sizeBar =
    withData.find((it) => it.props.dataKey === "size") ?? withData[withData.length - 1];
  const rects = sizeBar?.props.data;
  const vertical = props.verticalLayout === true;
  if (!rects || rects.length < 2) return null;
  const lines = [];
  for (let i = 0; i < rects.length - 1; i++) {
    const a = rects[i];
    const b = rects[i + 1];
    if (!a || !b) continue;
    if (vertical) {
      // Horizontal bars: the connector runs vertically at the value-end of
      // the next bar, from the end of bar i down to bar i+1.
      const x = b.x + b.width;
      lines.push(<line key={i} x1={x} y1={a.y + a.height} x2={x} y2={b.y} />);
    } else {
      // Vertical bars: the connector runs horizontally at the top of the
      // next bar, from the right edge of bar i to the left edge of bar i+1.
      const y = b.y;
      lines.push(<line key={i} x1={a.x + a.width} y1={y} x2={b.x} y2={y} />);
    }
  }
  return (
    <g stroke="rgb(var(--ink) / 0.55)" strokeWidth={1} strokeDasharray="3 3" aria-hidden="true">
      {lines}
    </g>
  );
}

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
      margin={
        compact ? { top: 6, right: 56, bottom: 0, left: 0 } : { top: 22, right: 4, bottom: 0, left: 4 }
      }
      barCategoryGap="22%"
    >
      <defs>
        {/* Leakage: garnet hatch. Costs: ink dots on a light wash. Pattern
            strokes are tuned to sit at or above 3:1 on the card in both
            themes (the tokens are theme-mapped; ratios in the design plan). */}
        <pattern
          id="drop-hatch"
          patternUnits="userSpaceOnUse"
          width="6"
          height="6"
          patternTransform="rotate(135)"
        >
          <rect width="6" height="6" fill="rgb(var(--garnet) / 0.2)" />
          <line x1="0" y1="0" x2="0" y2="6" stroke="rgb(var(--garnet) / 0.8)" strokeWidth="1.6" />
        </pattern>
        <pattern id="cost-dots" patternUnits="userSpaceOnUse" width="7" height="7">
          <rect width="7" height="7" fill="rgb(var(--ink) / 0.12)" />
          <circle cx="2.2" cy="2.2" r="1.3" fill="rgb(var(--ink) / 0.7)" />
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
      {/* Base bar: invisible spacer under totals; a faint ghost column under
          floating deduction bars so they read as sitting on the remainder. */}
      <Bar dataKey="base" stackId="w" isAnimationActive={false}>
        {rows.map((r) => (
          <Cell key={r.name} fill={r.kind === "stage" ? "transparent" : "rgb(var(--ink) / 0.05)"} />
        ))}
      </Bar>
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
      <Customized
        component={(p: Record<string, unknown>) => <Connectors {...p} verticalLayout={compact} />}
      />
    </BarChart>
  );
  return (
    <ResponsiveContainer width="100%" height={compact ? 360 : 264}>
      {chart}
    </ResponsiveContainer>
  );
}
