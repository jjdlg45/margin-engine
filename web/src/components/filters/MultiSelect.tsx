/** Multi-select filter (React Aria Components): a labeled popover listbox
 * with checkbox semantics and full keyboard support. Escape CLOSES the
 * popover and keeps the selection: React Aria's default for multi-select
 * listboxes is to clear the selection on Escape (destructive for a filter,
 * and RAC 1.7 has no escapeKeyBehavior prop yet), so Escape is intercepted
 * before the listbox sees it. */

import { useContext } from "react";
import {
  Button,
  Dialog,
  DialogTrigger,
  ListBox,
  ListBoxItem,
  OverlayTriggerStateContext,
  Popover,
  Selection,
} from "react-aria-components";

function EscapeCloses({ children }: { children: React.ReactNode }) {
  const state = useContext(OverlayTriggerStateContext);
  return (
    <div
      onKeyDownCapture={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          state?.close();
        }
      }}
    >
      {children}
    </div>
  );
}

export function MultiSelect({
  label,
  options,
  values,
  onChange,
  renderOption,
}: {
  label: string;
  options: string[];
  values: string[];
  onChange: (values: string[]) => void;
  renderOption?: (v: string) => string;
}) {
  const selected: Selection = new Set(values);
  return (
    <DialogTrigger>
      <div className="flex flex-col gap-1">
        <span className="text-caption secondary">{label}</span>
        <Button className="w-56 max-w-full rounded-badge border hairline bg-card px-2 py-1 text-left text-sm text-ink/[0.87]">
          {values.length ? `${values.length} selected` : "Any"}
        </Button>
      </div>
      <Popover className="z-20 rounded-card border hairline bg-card shadow-lg">
        <Dialog aria-label={label} className="outline-none">
          <EscapeCloses>
            <ListBox
              selectionMode="multiple"
              selectedKeys={selected}
              onSelectionChange={(keys) =>
                onChange(keys === "all" ? options : [...keys].map(String))
              }
              className="max-h-72 w-64 overflow-auto p-1"
            >
              {options.map((o) => (
                <ListBoxItem
                  key={o}
                  id={o}
                  textValue={renderOption ? renderOption(o) : o}
                  className="flex cursor-pointer items-center gap-2 rounded-badge px-2 py-1.5 text-sm data-[focused]:bg-petrol data-[focused]:text-paper"
                >
                  {({ isSelected }) => (
                    <>
                      <span
                        aria-hidden="true"
                        className={`inline-block h-3.5 w-3.5 rounded-sm border ${
                          isSelected
                            ? "border-petrol bg-petrol"
                            : "border-ink/40"
                        }`}
                      />
                      {renderOption ? renderOption(o) : o}
                    </>
                  )}
                </ListBoxItem>
              ))}
            </ListBox>
          </EscapeCloses>
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}
