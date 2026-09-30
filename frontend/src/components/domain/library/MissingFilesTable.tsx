import { useEffect, useRef } from "react";
import type { MissingFile } from "@/lib/schemas/missing";

interface MissingFilesTableProps {
  files: MissingFile[];
  selected: ReadonlySet<string>;
  onToggle: (id: string) => void;
  onToggleAll: () => void;
  onRemap: (file: MissingFile) => void;
  remapDisabled?: boolean;
}

const CELL = "px-3 py-2 text-gray-700";
const REMAP_BUTTON =
  "rounded-md border border-gray-300 px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50";
const HEADINGS = ["File", "Artist", "Title", "Release", "Missing since", "Work", "Matches"];

function missingSince(value: string | null): string {
  return value ? new Date(value).toLocaleDateString() : "—";
}

function pathCellId(file: MissingFile): string {
  return `missing-file-path-${file.id}`;
}

function WorkCell({ file }: { file: MissingFile }) {
  return (
    <td className={CELL}>
      {file.work_title ?? "—"}
      {file.work_id && !file.work_has_present_file && (
        <span className="ml-2 rounded bg-amber-100 px-1.5 text-xs text-amber-800">
          no copy on disk
        </span>
      )}
    </td>
  );
}

function Row({
  file,
  checked,
  onToggle,
  onRemap,
  remapDisabled,
}: {
  file: MissingFile;
  checked: boolean;
  onToggle: (id: string) => void;
  onRemap: (file: MissingFile) => void;
  remapDisabled: boolean;
}) {
  const remapLabel = file.candidates.length > 0 ? `Remap (${file.candidates.length})` : "Remap";
  return (
    <tr className="border-b border-gray-100 last:border-0">
      <td className="px-3 py-2">
        <input
          type="checkbox"
          aria-label={`Select ${file.file_path}`}
          checked={checked}
          onChange={() => onToggle(file.id)}
        />
      </td>
      <td id={pathCellId(file)} className="break-all px-3 py-2 font-mono text-xs text-gray-700">
        {file.file_path}
      </td>
      <td className={CELL}>{file.artist_name ?? "—"}</td>
      <td className={CELL}>{file.track_title ?? "—"}</td>
      <td className={CELL}>{file.release_title ?? "—"}</td>
      <td className={CELL}>{missingSince(file.missing_since)}</td>
      <WorkCell file={file} />
      <td className={CELL}>{file.match_count}</td>
      <td className="px-3 py-2 text-right">
        <button
          type="button"
          aria-describedby={pathCellId(file)}
          disabled={remapDisabled}
          onClick={() => onRemap(file)}
          className={REMAP_BUTTON}
        >
          {remapLabel}
        </button>
      </td>
    </tr>
  );
}

/** The page's select-all box: checked when every row is chosen, indeterminate when some are. */
function SelectAllBox({
  files,
  selected,
  onToggleAll,
}: Pick<MissingFilesTableProps, "files" | "selected" | "onToggleAll">) {
  const ref = useRef<HTMLInputElement>(null);
  const chosen = files.filter((f) => selected.has(f.id)).length;
  const allSelected = files.length > 0 && chosen === files.length;
  const someSelected = chosen > 0 && !allSelected;
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = someSelected;
  }, [someSelected]);
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label="Select every file on this page"
      checked={allSelected}
      onChange={onToggleAll}
    />
  );
}

export function MissingFilesTable({
  files,
  selected,
  onToggle,
  onToggleAll,
  onRemap,
  remapDisabled = false,
}: MissingFilesTableProps) {
  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-100 bg-gray-50 text-left text-gray-600">
            <th className="px-3 py-2.5">
              <SelectAllBox files={files} selected={selected} onToggleAll={onToggleAll} />
            </th>
            {HEADINGS.map((h) => (
              <th key={h} className="px-3 py-2.5 font-semibold">
                {h}
              </th>
            ))}
            <th className="px-3 py-2.5">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <Row
              key={f.id}
              file={f}
              checked={selected.has(f.id)}
              onToggle={onToggle}
              onRemap={onRemap}
              remapDisabled={remapDisabled}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
