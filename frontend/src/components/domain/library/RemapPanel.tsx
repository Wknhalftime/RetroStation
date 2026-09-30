import { useState } from "react";
import { SearchSlideOver } from "@/components/domain/matcher/SearchSlideOver";
import type { MissingFile } from "@/lib/schemas/missing";

interface RemapPanelProps {
  file: MissingFile;
  pending: boolean;
  error: string | null;
  onRemap: (targetFileId: string) => void;
  onCancel: () => void;
}

const BUTTON =
  "rounded-md border border-gray-300 bg-white px-2 py-1 text-xs hover:bg-gray-50 " +
  "disabled:opacity-50";

export function RemapPanel({ file, pending, error, onRemap, onCancel }: RemapPanelProps) {
  const [searching, setSearching] = useState(false);
  return (
    <section
      aria-label="Remap missing file"
      className="mt-6 rounded-xl border border-indigo-200 bg-indigo-50 p-4"
    >
      <h2 className="break-all text-sm font-semibold text-gray-900">Remap {file.file_path}</h2>
      <p className="mt-1 text-xs text-gray-600">
        Its matches, master pick and overrides move to the file you choose, and this row is removed.
      </p>
      {file.candidates.length === 0 ? (
        <p className="mt-3 text-xs text-gray-500">No present copy of this track was found.</p>
      ) : (
        <ul className="mt-3 space-y-1">
          {file.candidates.map((c) => (
            <li key={c.id} className="flex items-center justify-between gap-3">
              <span className="break-all font-mono text-xs">{c.file_path}</span>
              <button
                type="button"
                disabled={pending}
                onClick={() => onRemap(c.id)}
                className={BUTTON}
              >
                Use this file
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          Could not remap: {error}
        </p>
      )}
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          disabled={pending}
          onClick={() => setSearching(true)}
          className={BUTTON}
        >
          Search the library…
        </button>
        <button type="button" onClick={onCancel} className={BUTTON}>
          Cancel
        </button>
      </div>
      <SearchSlideOver
        open={searching}
        onClose={() => setSearching(false)}
        mode="file"
        onSelectFile={(target) => {
          setSearching(false);
          if (!pending) onRemap(target.id);
        }}
      />
    </section>
  );
}
