/** Evidence-status chip: distinct fill treatment + label + tooltip with the
 * reason (never color alone). Grammar: solid = supported, hatched = IV-level,
 * outline = descriptive, dashed = insufficient. */

import * as Tooltip from "@radix-ui/react-tooltip";

const TREATMENT: Record<string, string> = {
  causal_elasticity_supported: "chip-solid",
  estimated_win_curve: "chip-solid",
  iv_estimated_elasticity: "chip-hatched",
  descriptive_elasticity: "chip-outline",
  insufficient_data: "chip-dashed",
  insufficient_precision: "chip-dashed",
  in_band: "chip-solid",
  reconciled: "chip-solid",
  reconciliation_failed: "chip-fail",
  above_reference: "chip-hatched",
  below_reference: "chip-hatched",
  below_floor: "chip-fail",
  insufficient_pricing_headroom: "chip-fail",
  provisional_historical: "chip-outline",
};

const MEANING: Record<string, string> = {
  causal_elasticity_supported:
    "Every causal criterion passed: strong first stage, overidentification not rejected, robust across specifications, tight interval, documented exclusion rationale.",
  iv_estimated_elasticity:
    "Instrumented estimate with an adequate first stage, but at least one causal criterion failed - treat as directional.",
  descriptive_elasticity:
    "Estimable association only; no causal claim is supported.",
  insufficient_data: "Too little data to support any estimate - fails closed.",
  insufficient_precision:
    "The fit converged but its own standard error cannot support the claimed accuracy - reported, not gated.",
  in_band: "Inside the segment's historical reference band.",
  reconciled:
    "All reconciliation checks pass: ledger totals equal source totals exactly after rounding.",
  reconciliation_failed: "At least one reconciliation check failed.",
  below_reference: "Below the segment's historical reference band.",
  above_reference: "Above the segment's historical reference band.",
  below_floor: "Below the economic floor that guarantees the minimum pocket margin.",
  insufficient_pricing_headroom:
    "The floor sits at or above the reference band; the corridor is infeasible.",
  provisional_historical:
    "A historical position, not an optimal price - provisional until elasticity evidence supports optimization.",
};

export function StatusChip({ status, reason }: { status: string; reason?: string }) {
  const cls = TREATMENT[status] ?? "chip-outline";
  const tip = [MEANING[status], reason].filter(Boolean).join(" ");
  const chip = <span className={`chip ${cls}`}>{status.replace(/_/g, " ")}</span>;
  if (!tip) return chip;
  return (
    <Tooltip.Root delayDuration={150}>
      <Tooltip.Trigger asChild>
        <button className="cursor-help border-0 bg-transparent p-0" aria-label={`${status}: ${tip}`}>
          {chip}
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          className="z-10 max-w-xs rounded-card bg-ink px-3 py-2 text-caption text-paper shadow-lg"
          sideOffset={4}
        >
          {tip}
          <Tooltip.Arrow className="fill-ink" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
