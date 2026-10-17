/** Shared page scaffolding: title with the accent rule, a one-paragraph lede,
 * and a consistent empty/edge-state block that explains what happened in
 * plain language (spec: edge states are designed, not blank). */

export function PageHeader({ title, lede }: { title: string; lede: React.ReactNode }) {
  return (
    <header className="pb-6 pt-10">
      <h1 className="heading-rule m-0 text-title">{title}</h1>
      <p className="mb-0 mt-3 max-w-2xl secondary">{lede}</p>
    </header>
  );
}

export function Section({
  title,
  children,
  lede,
}: {
  title: string;
  lede?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="py-8">
      <h2 className="heading-rule mb-4 mt-0 text-section">{title}</h2>
      {lede && <p className="mb-5 mt-0 max-w-2xl text-caption secondary">{lede}</p>}
      {children}
    </section>
  );
}

export function EmptyState({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-[8px] border border-dashed border-ink/[0.38] px-5 py-6">
      <p className="m-0 font-medium">{title}</p>
      <p className="mb-0 mt-1 max-w-xl text-caption secondary">{children}</p>
    </div>
  );
}

/** Horizontally scrollable region that stays keyboard-accessible: focusable
 * with a name, so axe's scrollable-region-focusable passes honestly. */
export function ScrollX({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto" tabIndex={0} role="region" aria-label={label}>
      {children}
    </div>
  );
}
