import { useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { VersionBadges } from "@/components/domain/works/VersionBadges";
import type { WorkSummary } from "@/lib/schemas/artists";

interface MergeWorksDialogProps {
  open: boolean;
  onClose: () => void;
  /** Title of the work being kept: the one whose page is open. */
  keepTitle: string;
  /** The artist's other works, any of which can be folded into the kept one. */
  candidates: WorkSummary[];
  onMerge: (sourceWorkIds: string[]) => void;
  isMerging: boolean;
  /** Why the last merge was refused, if it was. */
  error: string | null;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** Pick duplicate works to fold into the open one. */
export function MergeWorksDialog({
  open,
  onClose,
  keepTitle,
  candidates,
  onMerge,
  isMerging,
  error,
}: MergeWorksDialogProps) {
  const [filter, setFilter] = useState("");
  // In pick order; a pick stays picked while the filter hides it.
  const [picked, setPicked] = useState<string[]>([]);

  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? candidates.filter((w) => w.title.toLowerCase().includes(needle))
    : candidates;

  const toggle = (workId: string) =>
    setPicked((prev) =>
      prev.includes(workId) ? prev.filter((id) => id !== workId) : [...prev, workId]
    );

  return (
    <Modal open={open} onClose={onClose} title="Merge duplicate works">
      <div className="space-y-4 text-sm">
        <p className="text-gray-600">
          Pick the works that are the same song as{" "}
          <span className="font-medium text-gray-900">{keepTitle}</span>. Their files, recordings
          and matches move to it, and the picked works are deleted. To undo, each file has to be
          split back out on its own.
        </p>

        {candidates.length === 0 ? (
          <p className="rounded-md bg-gray-50 px-3 py-2 text-gray-500">
            This artist has no other works to merge.
          </p>
        ) : (
          <>
            <input
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter by title"
              aria-label="Filter works by title"
              className="w-full rounded-md border border-gray-300 px-3 py-1.5 focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
            <ul className="max-h-72 divide-y divide-gray-100 overflow-y-auto rounded-md border border-gray-200">
              {shown.map((work) => (
                <li key={work.id}>
                  <label className="flex cursor-pointer items-center gap-3 px-3 py-2 hover:bg-gray-50">
                    <input
                      type="checkbox"
                      aria-label={work.title}
                      checked={picked.includes(work.id)}
                      onChange={() => toggle(work.id)}
                      className="h-4 w-4 rounded border-gray-300 text-indigo-600"
                    />
                    <span className="min-w-0 flex-1 truncate text-gray-900">{work.title}</span>
                    <VersionBadges versionTypes={work.version_types} />
                    <span className="shrink-0 text-xs text-gray-400">
                      {plural(work.recording_count, "recording")}
                    </span>
                  </label>
                </li>
              ))}
              {shown.length === 0 && (
                <li className="px-3 py-2 text-gray-400">No works match the filter.</li>
              )}
            </ul>
          </>
        )}

        {error && (
          <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-red-700">
            Could not merge: {error}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-gray-300 bg-white px-3 py-1.5 font-medium text-gray-700 hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onMerge(picked)}
            disabled={picked.length === 0 || isMerging}
            className="rounded-md bg-red-600 px-3 py-1.5 font-medium text-white hover:bg-red-700 disabled:opacity-50"
          >
            {isMerging ? "Merging..." : `Merge ${plural(picked.length, "work")} into this one`}
          </button>
        </div>
      </div>
    </Modal>
  );
}
