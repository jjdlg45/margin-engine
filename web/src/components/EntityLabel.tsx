/** Name-first entity display: "Gulf Coast Mechanical Supply · C10031".
 * Falls back to the ID alone when the export carries no names. */

export function EntityLabel({ name, id }: { name: string | null | undefined; id: string }) {
  if (!name) return <span className="font-mono">{id}</span>;
  return (
    <span>
      {name} <span className="font-mono text-caption secondary">{"·"} {id}</span>
    </span>
  );
}

export function entityText(name: string | null | undefined, id: string): string {
  return name ? `${name} · ${id}` : id;
}
