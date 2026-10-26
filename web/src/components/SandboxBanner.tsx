/** Demo-sandbox banner (polish round): tells the visitor their workflow
 * changes live only in this browser, and offers a reset. Rendered wherever
 * the sandbox workflow is available (Action center and action detail). */

import { isSandbox } from "../lib/api/client";
import { resetSandbox } from "../lib/api/sandboxAdapter";
import { queryClient } from "../lib/queries";

export function SandboxBanner() {
  if (!isSandbox) return null;
  const reset = async () => {
    await resetSandbox();
    await queryClient.invalidateQueries();
  };
  return (
    <div
      className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-[8px] border border-amber/40 bg-amber/5 px-4 py-3"
      data-testid="sandbox-banner"
    >
      <p className="m-0 text-caption">
        <span className="font-semibold">Demo sandbox:</span> your changes stay on this device.
        Propose, review, approve and export freely - the same guardrails as the live workflow
        apply, and every dollar figure still comes from the engine.
      </p>
      <button
        type="button"
        onClick={() => void reset()}
        className="rounded-badge border hairline bg-card px-2 py-1 text-sm text-ink/[0.87] hover:text-ink"
        data-testid="sandbox-reset"
      >
        Reset demo
      </button>
    </div>
  );
}
