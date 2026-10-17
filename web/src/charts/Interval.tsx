/** Per-segment elasticity intervals. The point marker follows the evidence
 * grammar: solid = causal-supported, hatched (half-filled) = IV-level,
 * outline = descriptive, dashed = insufficient. The naive estimate renders as
 * a small open square for the bias comparison. */

import { num } from "../lib/format";
import type { SensitivitySegment } from "../types/bundle";

export function IntervalChart({ rows }: { rows: SensitivitySegment[] }) {
  const withIv = rows.filter((r) => r.iv !== null);
  const values = withIv.flatMap((r) =>
    [r.iv, r.iv_ci_low, r.iv_ci_high, r.naive_fe_ols].filter(
      (v): v is number => v !== null && isFinite(v),
    ),
  );
  if (!values.length) return null;
  const lo = Math.min(...values, -0.2) - 0.2;
  const hi = Math.max(...values, 0.2) + 0.2;
  const W = 640;
  const rowH = 34;
  const H = withIv.length * rowH + 30;
  const x = (v: number) => ((v - lo) / (hi - lo)) * (W - 150) + 130;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="h-auto w-full max-w-[640px]"
      role="img"
      aria-label="Elasticity point estimates with 95% intervals per segment"
    >
      <line x1={x(0)} x2={x(0)} y1={6} y2={H - 24} stroke="rgb(var(--ink) / 0.25)" strokeDasharray="2 3" />
      <text x={x(0)} y={H - 10} textAnchor="middle" fontSize={10} fill="rgb(var(--ink) / 0.7)">
        0
      </text>
      {withIv.map((r, i) => {
        const y = i * rowH + 22;
        const causal = r.status === "causal_elasticity_supported";
        const ivLevel = r.status === "iv_estimated_elasticity";
        return (
          <g key={r.segment}>
            <text x={4} y={y + 4} fontSize={11} fill="rgb(var(--ink) / 0.87)">
              Segment {r.segment}
            </text>
            {r.iv_ci_low !== null && r.iv_ci_high !== null && (
              <line
                x1={x(r.iv_ci_low)}
                x2={x(r.iv_ci_high)}
                y1={y}
                y2={y}
                stroke="rgb(var(--ink) / 0.55)"
                strokeWidth={2}
                strokeDasharray={causal || ivLevel ? undefined : "3 3"}
              />
            )}
            {r.naive_fe_ols !== null && (
              <rect
                x={x(r.naive_fe_ols) - 3.5}
                y={y - 3.5}
                width={7}
                height={7}
                fill="none"
                stroke="rgb(var(--amber))"
                strokeWidth={1.5}
              />
            )}
            {r.iv !== null && (
              <circle
                cx={x(r.iv)}
                cy={y}
                r={5}
                fill={causal ? "rgb(var(--verdigris))" : ivLevel ? "rgb(var(--amber) / 0.45)" : "transparent"}
                stroke={causal ? "rgb(var(--verdigris))" : ivLevel ? "rgb(var(--amber))" : "rgb(var(--ink) / 0.7)"}
                strokeWidth={1.8}
              />
            )}
            {r.iv !== null && (
              <text x={W - 6} y={y + 4} textAnchor="end" fontSize={11} fill="rgb(var(--ink) / 0.87)">
                {num(r.iv)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
