/** Process step tracker (polish round): where an action stands in the whole
 * pricing process, including the step that happens OUTSIDE this tool (the ERP
 * import of the exported price file). Never color alone: done steps carry a
 * filled marker plus screen-reader text, the current step is bold with
 * aria-current="step". */

const STEPS: { label: string; status: string | null; outside?: boolean }[] = [
  { label: "Proposed", status: "proposed" },
  { label: "Under review", status: "under_review" },
  { label: "Approved", status: "approved" },
  { label: "Exported", status: "exported" },
  { label: "Loaded into ERP", status: null, outside: true },
  { label: "Implemented", status: "implemented" },
  { label: "Measured", status: "measured" },
];

/** Index of the current step per lifecycle status; the ERP-load step (4) is
 * never "current" from the tool's side - implemented implies it happened. */
const CURRENT: Record<string, number> = {
  proposed: 0,
  under_review: 1,
  approved: 2,
  exported: 3,
  implemented: 5,
  measured: 6,
};

export function StepTracker({ status }: { status: string }) {
  const rejected = status === "rejected";
  // A rejected action sits at review; rework returns it to proposed.
  const current = rejected ? 1 : (CURRENT[status] ?? 0);
  return (
    <div data-testid="step-tracker">
      <ol className="m-0 flex list-none flex-wrap gap-x-5 gap-y-2 p-0 text-caption">
        {STEPS.map((s, i) => {
          const done = i < current;
          const isCurrent = i === current && !rejected;
          return (
            <li
              key={s.label}
              className="flex items-center gap-1.5"
              aria-current={isCurrent ? "step" : undefined}
            >
              <span
                aria-hidden="true"
                className={
                  done
                    ? "h-2 w-2 rounded-full bg-petrol"
                    : isCurrent
                      ? "h-2.5 w-2.5 rounded-full border-2 border-petrol"
                      : "h-2 w-2 rounded-full border hairline"
                }
              />
              <span
                className={
                  isCurrent ? "font-semibold text-ink" : done ? "text-ink/[0.87]" : "secondary"
                }
              >
                {s.label}
                {done && <span className="sr-only"> (done)</span>}
                {isCurrent && <span className="sr-only"> (current step)</span>}
              </span>
              {s.outside && <span className="secondary">(in your ERP)</span>}
            </li>
          );
        })}
      </ol>
      {rejected && (
        <p className="mb-0 mt-2 text-caption text-garnet">
          Rejected at review. Rework returns it to Proposed with the reviewer's reason attached.
        </p>
      )}
    </div>
  );
}
