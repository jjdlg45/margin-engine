/** Searchable combobox (React Aria Components): type-ahead over names and
 * IDs with full keyboard navigation, plus a lookup-mode toggle that switches
 * the primary text between Name and ID. Styled with the project tokens; no
 * native select elements. */

import { useMemo, useState } from "react";
import {
  Button,
  ComboBox,
  Header,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  ListBoxSection,
  Popover,
} from "react-aria-components";

export interface Option {
  id: string;
  name: string | null;
  /** Optional group heading (e.g. Category / Supplier on scenarios). */
  group?: string;
  /** Optional right-aligned hint, e.g. a pair count. */
  hint?: string;
}

export function EntityCombobox({
  label,
  options,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  options: Option[];
  value: string | null;
  onChange: (id: string | null) => void;
  placeholder?: string;
}) {
  const hasNames = options.some((o) => o.name);
  const [byId, setById] = useState(!hasNames);
  const display = (o: Option) =>
    byId || !o.name ? `${o.id}${o.name ? ` · ${o.name}` : ""}` : `${o.name} · ${o.id}`;
  const items = useMemo(
    () => options.map((o) => ({ ...o, text: display(o), search: `${o.name ?? ""} ${o.id}` })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [options, byId],
  );
  const selected = items.find((o) => o.id === value) ?? null;
  return (
    <ComboBox
      className="flex flex-col gap-1"
      selectedKey={value}
      onSelectionChange={(k) => onChange(k === null ? null : String(k))}
      defaultFilter={(textValue, input) => {
        const item = items.find((i) => i.text === textValue);
        const hay = (item ? item.search : textValue).toLowerCase();
        return input
          .toLowerCase()
          .split(/\s+/)
          .every((tok) => hay.includes(tok));
      }}
      menuTrigger="focus"
      allowsEmptyCollection
    >
      <div className="flex items-center gap-2">
        <Label className="text-caption secondary">{label}</Label>
        {hasNames && (
          <button
            type="button"
            onClick={() => setById(!byId)}
            className="rounded-badge border hairline bg-card px-1.5 py-0.5 text-caption secondary hover:text-ink"
            aria-label={`Look up by ${byId ? "name" : "ID"}`}
          >
            {byId ? "By ID" : "By name"}
          </button>
        )}
      </div>
      <div className="flex w-72 max-w-full items-stretch">
        <Input
          placeholder={placeholder ?? (selected ? selected.text : "Type to search…")}
          className="w-full rounded-l-badge border hairline bg-card px-2 py-1 text-sm text-ink/[0.87]"
        />
        <Button className="rounded-r-badge border hairline border-l-0 bg-card px-2 text-ink/70">
          {"▾"}
        </Button>
      </div>
      <Popover className="z-20 max-h-72 w-[var(--trigger-width)] overflow-auto rounded-card border hairline bg-card shadow-lg">
        <ListBox
          renderEmptyState={() => <p className="px-3 py-2 text-caption secondary">No matches.</p>}
        >
          {(() => {
            const item = (o: (typeof items)[number]) => (
              <ListBoxItem
                key={o.id}
                id={o.id}
                textValue={o.text}
                className="flex cursor-pointer items-baseline justify-between gap-3 px-3 py-1.5 text-sm data-[focused]:bg-petrol data-[focused]:text-paper"
              >
                <span>{o.text}</span>
                {o.hint && <span className="text-caption opacity-70">{o.hint}</span>}
              </ListBoxItem>
            );
            const groups = [...new Set(items.map((o) => o.group).filter(Boolean))] as string[];
            if (!groups.length) return items.map(item);
            return groups.map((g) => (
              <ListBoxSection key={g} id={g}>
                <Header className="px-3 pb-0.5 pt-2 text-caption font-semibold secondary">
                  {g}
                </Header>
                {items.filter((o) => o.group === g).map(item)}
              </ListBoxSection>
            ));
          })()}
        </ListBox>
      </Popover>
    </ComboBox>
  );
}
