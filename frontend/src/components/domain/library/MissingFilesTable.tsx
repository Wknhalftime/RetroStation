import type { MissingFile } from "@/lib/schemas/missing";

interface MissingFilesTableProps {
  files: MissingFile[];
  selected: ReadonlySet<string>;
  onToggle: (id: string) => void;
  onToggleAll: () => void;
  onRemap: (file: MissingFile) => void;
}

const CELL = "px-3 py-2 text-gray-700";

function missingSince(value: string | null): string {
  return value ? new Date(value).toLocaleDateString() : "—";
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
}: {
  file: MissingFile;
  checked: boolean;
  onToggle: (id: string) => void;
  onRemap: (file: MissingFile) => void;
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
      <td className="break-all px-3 py-2 font-mono text-xs text-gray-700">{file.file_path}</td>
      <td className={CELL}>{file.artist_name ?? "—"}</td>
      <td className={CELL}>{file.track_title ?? "—"}</td>
      <td className={CELL}>{file.release_title ?? "—"}</td>
      <td className={CELL}>{missingSince(file.missing_since)}</td>
      <WorkCell file={file} />
      <td className={CELL}>{file.match_count}</td>
      <td className="px-3 py-2 text-right">
        <button
          type="button"
          onClick={() => onRemap(file)}
          className="rounded-md border border-gray-300 px-2 py-1 text-xs hover:bg-gray-50"
        >
          {remapLabel}
        </button>
      </td>
    </tr>
  );
}

export function MissingFilesTable({
  files,
  selected,
  onToggle,
  onToggleAll,
  onRemap,
}: MissingFilesTableProps) {
  const allSelected = files.length > 0 && files.every((f) => selected.has(f.id));
  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-100 bg-gray-50 text-left text-gray-600">
            <th className="px-3 py-2.5">
              <input
                type="checkbox"
                aria-label="Select every file on this page"
                checked={allSelected}
                onChange={onToggleAll}
              />
            </th>
            {["File", "Artist", "Title", "Release", "Missing since", "Work", "Matches", ""].map(
              (h) => (
                <th key={h} className="px-3 py-2.5 font-semibold">
                  {h}
                </th>
              )
            )}
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
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
