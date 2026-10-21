/** Multi-select filter (React Aria Components): a labeled popover listbox
 * with checkbox semantics and full keyboard support. */

import {
  Button,
  Dialog,
  DialogTrigger,
  ListBox,
  ListBoxItem,
  Popover,
  Selection,
} from "react-aria-components";

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
                        isSelected ? "border-petrol bg-petrol" : "border-ink/40"
                      }`}
                    />
                    {renderOption ? renderOption(o) : o}
                  </>
                )}
              </ListBoxItem>
            ))}
          </ListBox>
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}
