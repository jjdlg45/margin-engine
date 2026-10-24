/** Searchable combobox (React Aria Components): type-ahead over names and
 * IDs with full keyboard navigation, plus a lookup-mode toggle that switches
 * the primary text between Name and ID. Styled with the project tokens; no
 * native select elements. Filtering is controlled (the component filters its
 * own options), so option lists of 100 or more are virtualized with an exact
 * height - only visible options exist in the DOM. */

import { useMemo, useState } from "react";
import {
  Button,
  Collection,
  ComboBox,
  Header,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  ListBoxSection,
  ListLayout,
  Popover,
  Virtualizer,
} from "react-aria-components";

export interface Option {
  id: string;
  name: string | null;
  /** Optional group heading (e.g. Category / Supplier on scenarios). */
  group?: string;
  /** Optional right-aligned hint, e.g. a pair count. */
  hint?: string;
}

const ROW_PX = 32;
const HEADER_PX = 28;
const MAX_LIST_PX = 288; // matches the previous max-h-72
const MIN_LIST_PX = 160; // an open list is never a sliver: ~5 rows minimum
const EMPTY_PX = 40; // room for the "No matches." empty state

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
  const [inputValue, setInputValue] = useState("");
  // An external clear (Clear all, chip removal) must not leave stale text in
  // the input: when the selected value is cleared from outside, reset the
  // typed text in the same render (state adjustment during render, so the
  // stale text never paints).
  const [prevValue, setPrevValue] = useState(value);
  if (prevValue !== value) {
    setPrevValue(value);
    if (value === null && inputValue !== "") setInputValue("");
  }
  const display = (o: Option) =>
    byId || !o.name ? `${o.id}${o.name ? ` · ${o.name}` : ""}` : `${o.name} · ${o.id}`;
  const items = useMemo(
    () => options.map((o) => ({ ...o, text: display(o), search: `${o.name ?? ""} ${o.id}` })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [options, byId],
  );
  // Controlled filtering: with items and inputValue both controlled, React
  // Aria applies no filter of its own, so the filtered count is known here
  // and the virtualized list can be sized exactly.
  const filtered = useMemo(() => {
    const tokens = inputValue.toLowerCase().split(/\s+/).filter((t) => t && t !== "·");
    if (!tokens.length) return items;
    return items.filter((o) => {
      const hay = o.search.toLowerCase();
      return tokens.every((t) => hay.includes(t));
    });
  }, [items, inputValue]);
  const selected = items.find((o) => o.id === value) ?? null;
  const groups = useMemo(
    () => [...new Set(filtered.map((o) => o.group).filter(Boolean))] as string[],
    [filtered],
  );
  // Virtualize at 100+ options (the project threshold is "over 100"; the
  // boundary is included so the demo's 100-item list exercises the path).
  const virtual = items.length >= 100;
  const listLayout = useMemo(
    () => new ListLayout({ rowHeight: ROW_PX, headingHeight: HEADER_PX }),
    [],
  );
  const listHeight = Math.min(
    filtered.length * ROW_PX + groups.length * HEADER_PX,
    MAX_LIST_PX,
  );
  // The open list must be visibly tall wherever the input sits on screen,
  // even when the overlay positioner computes a collapsed available height
  // (zero-height open state seen on Linux CI at 375 px). CSS min-height
  // overrides the positioner's inline max-height; the popover still flips
  // above the input when the space below is short.
  const minVisiblePx = filtered.length ? Math.min(listHeight, MIN_LIST_PX) : EMPTY_PX;

  const itemClass =
    "flex cursor-pointer items-baseline justify-between gap-3 px-3 py-1.5 text-sm data-[focused]:bg-petrol data-[focused]:text-paper";
  const renderItem = (o: (typeof items)[number]) => (
    <ListBoxItem id={o.id} textValue={o.text} className={itemClass}>
      <span>{o.text}</span>
      {o.hint && <span className="text-caption opacity-70">{o.hint}</span>}
    </ListBoxItem>
  );
  const empty = () => <p className="px-3 py-2 text-caption secondary">No matches.</p>;
  const listBox = groups.length ? (
    <ListBox
      className="overflow-auto"
      style={
        virtual
          ? { height: Math.max(listHeight, minVisiblePx) }
          : { maxHeight: MAX_LIST_PX, minHeight: minVisiblePx }
      }
      items={groups.map((g) => ({ id: g, opts: filtered.filter((o) => o.group === g) }))}
      renderEmptyState={empty}
    >
      {(section) => (
        <ListBoxSection id={section.id}>
          <Header className="px-3 pb-0.5 pt-2 text-caption font-semibold secondary">
            {section.id}
          </Header>
          <Collection items={section.opts}>{renderItem}</Collection>
        </ListBoxSection>
      )}
    </ListBox>
  ) : (
    <ListBox
      className="overflow-auto"
      style={
        virtual
          ? { height: Math.max(listHeight, minVisiblePx) }
          : { maxHeight: MAX_LIST_PX, minHeight: minVisiblePx }
      }
      items={filtered}
      renderEmptyState={empty}
    >
      {renderItem}
    </ListBox>
  );

  return (
    <ComboBox
      className="flex flex-col gap-1"
      selectedKey={value}
      onSelectionChange={(k) => onChange(k === null ? null : String(k))}
      inputValue={inputValue}
      onInputChange={setInputValue}
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
      <Popover
        className="z-20 w-[var(--trigger-width)] rounded-card border hairline bg-card shadow-lg"
        style={{ minHeight: minVisiblePx }}
      >
        {virtual ? <Virtualizer layout={listLayout}>{listBox}</Virtualizer> : listBox}
      </Popover>
    </ComboBox>
  );
}
