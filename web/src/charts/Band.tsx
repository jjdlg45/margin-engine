/** Guidance band chart: the segment's historical reference band (tinted area
 * P25-P75 with a P50 midline), the economic floor as a solid garnet rule, and
 * the customer's current price as a 2px ink tick. Pure SVG, values from the
 * bundle only. */

import { num } from "../lib/format";

export function BandChart({
  p25,
  p50,
  p75,
  floor,
  current,
  target,
}: {
  p25: number | null;
  p50: number | null;
  p75: number | null;
  floor: number | null;
  current: number | null;
  target: number | null;
}) {
  const values = [p25, p50, p75, floor, current, target].filter(
    (v): v is number => v !== null && isFinite(v),
  );
  if (!values.length) return null;
  const lo = Math.min(...values) - 0.06;
  const hi = Math.max(...values) + 0.06;
  const W = 560;
  const H = 92;
  const x = (v: number) => ((v - lo) / (hi - lo)) * (W - 40) + 20;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="h-auto w-full max-w-[560px]"
      role="img"
      aria-label={`Price index band chart: band ${num(p25)} to ${num(p75)}, midpoint ${num(
        p50,
      )}, floor ${num(floor)}, current ${num(current)}`}
    >
      <line x1={20} x2={W - 20} y1={54} y2={54} stroke="rgb(var(--ink) / 0.12)" />
      {p25 !== null && p75 !== null && (
        <rect
          x={x(p25)}
          width={Math.max(x(p75) - x(p25), 2)}
          y={34}
          height={40}
          fill="rgb(var(--petrol) / 0.16)"
        />
      )}
      {p50 !== null && (
        <line x1={x(p50)} x2={x(p50)} y1={34} y2={74} stroke="rgb(var(--petrol))" strokeWidth={1.5} />
      )}
      {floor !== null && (
        <>
          <line x1={x(floor)} x2={x(floor)} y1={26} y2={82} stroke="rgb(var(--garnet))" strokeWidth={2} />
          <text x={x(floor)} y={18} textAnchor="middle" fontSize={10} fill="rgb(var(--garnet))">
            floor {num(floor)}
          </text>
        </>
      )}
      {target !== null && (
        <line
          x1={x(target)}
          x2={x(target)}
          y1={34}
          y2={74}
          stroke="rgb(var(--ink) / 0.55)"
          strokeWidth={1}
          strokeDasharray="3 3"
        />
      )}
      {current !== null && (
        <>
          <line x1={x(current)} x2={x(current)} y1={28} y2={80} stroke="rgb(var(--ink))" strokeWidth={2.5} />
          <text x={x(current)} y={H - 1} textAnchor="middle" fontSize={10} fill="rgb(var(--ink) / 0.87)">
            current {num(current)}
          </text>
        </>
      )}
      {p25 !== null && (
        <text x={x(p25)} y={30} textAnchor="middle" fontSize={9} fill="rgb(var(--ink) / 0.7)">
          {num(p25)}
        </text>
      )}
      {p75 !== null && (
        <text x={x(p75)} y={30} textAnchor="middle" fontSize={9} fill="rgb(var(--ink) / 0.7)">
          {num(p75)}
        </text>
      )}
    </svg>
  );
}
