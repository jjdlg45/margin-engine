/** One headline per action, shared by every surface that shows one - the
 * queue, the Recommended page, the detail page and the export preview - so
 * an action can never introduce itself differently in two places.
 *
 * Display names lead; the ID appears only when the export carries no name
 * for the entity. A price action states the size of its move - the percent
 * and the new price - because "Raise price" without a number is a headline
 * that makes the reader open the row to learn what it proposes. */

import type { ActionRow } from "../types/bundle";
import { dollars } from "./format";

const PRICE_TYPES = new Set(["price_change", "below_floor_fix"]);

/** The proposed move as a fraction, or null when prices are missing. */
export function moveFraction(a: ActionRow): number | null {
  if (
    a.current_price_cents !== null &&
    a.current_price_cents !== undefined &&
    a.current_price_cents > 0 &&
    a.proposed_price_cents !== null &&
    a.proposed_price_cents !== undefined
  ) {
    return a.proposed_price_cents / a.current_price_cents - 1;
  }
  return null;
}

function pctText(fraction: number): string {
  return `${(Math.abs(fraction) * 100).toFixed(1)}%`;
}

/** "Sun Valley Mechanical Supply, 1/2 in. PVC Relay Switch" - names first,
 * an ID only where no name exists. IDs as secondary text belong on the
 * detail page, not in a headline. */
export function actionEntityNames(a: ActionRow): string | null {
  if (!a.customer_id) return null;
  const parts = [a.customer_name ?? a.customer_id];
  if (a.item_id) parts.push(a.item_name ?? a.item_id);
  return parts.join(", ");
}

/** The headline. `typeLabel` is the plain-English label for a.action_type
 * (from the label dictionary; passed in so this stays a pure function). */
export function actionHeadline(a: ActionRow, typeLabel: string): string {
  const names = actionEntityNames(a);
  if (PRICE_TYPES.has(a.action_type) && names) {
    const fraction = moveFraction(a);
    if (fraction !== null && a.proposed_price_cents !== null && a.proposed_price_cents !== undefined) {
      const verb = fraction >= 0 ? "Raise" : "Lower";
      const qualifier = a.staged_step
        ? " (step 1 of the move to target)"
        : a.action_type === "below_floor_fix"
          ? " (fix a below-floor price)"
          : "";
      return `${verb} price ${pctText(fraction)} to ${dollars(a.proposed_price_cents)}${qualifier}: ${names}`;
    }
  }
  if (names) return `${typeLabel}: ${names}`;
  // No customer scope (cost pass-through and the like): the generated title
  // already describes the scope.
  return a.title;
}
