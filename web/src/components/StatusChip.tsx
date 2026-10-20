/** Evidence-status chip: distinct fill treatment + human label + tooltip with
 * the meaning and the raw code (never color alone; never raw codes in visible
 * text). Grammar: solid = supported, hatched = IV-level, outline =
 * descriptive, dashed = insufficient. */

import * as Tooltip from "@radix-ui/react-tooltip";
import { useLabels } from "../lib/labels";

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
  expected_change: "chip-solid",
  sensitivity_range_only: "chip-hatched",
  fixed_volume_only: "chip-outline",
  minimum_order_policy: "chip-hatched",
  delivery_frequency_policy: "chip-hatched",
};

export function StatusChip({ status, reason }: { status: string; reason?: string }) {
  const labels = useLabels();
  const entry = labels(status);
  const cls = TREATMENT[status] ?? "chip-outline";
  const chip = <span className={`chip ${cls}`}>{entry.label}</span>;
  const tip = [entry.description, reason].filter(Boolean).join(" ");
  return (
    <Tooltip.Root delayDuration={150}>
      <Tooltip.Trigger asChild>
        <button
          className="cursor-help border-0 bg-transparent p-0"
          aria-label={`${entry.label}. ${tip}`}
        >
          {chip}
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          className="z-10 max-w-xs rounded-card bg-ink px-3 py-2 text-caption text-paper shadow-lg"
          sideOffset={4}
        >
          {tip && <span>{tip} </span>}
          <span className="font-mono opacity-70">[{status}]</span>
          <Tooltip.Arrow className="fill-ink" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
