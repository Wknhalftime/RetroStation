import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import {
  useDeleteMissingFiles,
  useMissingFiles,
  useRemapMissingFile,
  type DeleteMissingFilesBody,
} from "@/api/missing";
import { MissingFilesTable } from "@/components/domain/library/MissingFilesTable";
import { RemapPanel } from "@/components/domain/library/RemapPanel";
import { deletionPrompt } from "@/components/domain/library/deletionPrompt";
import type { MissingFile, MissingFileDeletion } from "@/lib/schemas/missing";

const PAGE_SIZE = 50;
const ACTION =
  "rounded-md bg-red-600 px-3 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50";

function toggled(set: ReadonlySet<string>, id: string): ReadonlySet<string> {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

function matchesOf(files: MissingFile[], ids: ReadonlySet<string>): number {
  return files.filter((f) => ids.has(f.id)).reduce((sum, f) => sum + f.match_count, 0);
}

function DeletionNote({ result }: { result: MissingFileDeletion | undefined }) {
  if (!result) return null;
  return (
    <p role="status" className="mb-4 rounded-md bg-green-50 p-3 text-sm text-green-800">
      Deleted {result.deleted}; {result.matches_released} matches released
      {result.skipped > 0 ? `; ${result.skipped} skipped (no longer missing)` : ""}.
    </p>
  );
}

function DeletionError({ error }: { error: Error | null }) {
  if (!error) return null;
  return (
    <p role="alert" className="mb-4 rounded-md bg-red-50 p-3 text-sm text-red-700">
      Could not delete: {error.message}
    </p>
  );
}

export function MissingFiles() {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [remapping, setRemapping] = useState<MissingFile | null>(null);
  const { data, isLoading, isError } = useMissingFiles(offset, PAGE_SIZE);
  const deletion = useDeleteMissingFiles();
  const remap = useRemapMissingFile();

  if (isLoading) return <Spinner className="mx-auto my-16 h-8 w-8 text-indigo-500" />;
  if (isError || !data)
    return <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">Failed to load.</p>;

  const files = data.items;
  const confirmThenDelete = (body: DeleteMissingFilesBody, count: number, matches: number) => {
    if (!window.confirm(deletionPrompt(count, matches))) return;
    deletion.mutate(body, { onSuccess: () => setSelected(new Set()) });
  };
  const turnPage = (next: number) => {
    setSelected(new Set());
    setOffset(next);
  };
  // A refusal belongs to the row it was raised for; opening or closing a panel clears it.
  const openRemap = (file: MissingFile | null) => {
    remap.reset();
    setRemapping(file);
  };

  return (
    <div>
      <PageHeader
        title="Missing Files"
        description={`${data.total} indexed files are no longer on disk.`}
        actions={
          <>
            <button
              type="button"
              className={ACTION}
              disabled={selected.size === 0 || deletion.isPending}
              onClick={() =>
                confirmThenDelete({ ids: [...selected] }, selected.size, matchesOf(files, selected))
              }
            >
              Delete selected
            </button>
            <button
              type="button"
              className={ACTION}
              disabled={data.total === 0 || deletion.isPending}
              onClick={() => confirmThenDelete({ all: true }, data.total, data.total_match_count)}
            >
              Delete all
            </button>
          </>
        }
      />
      <DeletionNote result={deletion.data} />
      <DeletionError error={deletion.error} />
      {files.length === 0 ? (
        <EmptyState title="No missing files" description="Every indexed file is on disk." />
      ) : (
        <MissingFilesTable
          files={files}
          selected={selected}
          onToggle={(id) => setSelected((s) => toggled(s, id))}
          onToggleAll={() =>
            setSelected((s) =>
              files.every((f) => s.has(f.id)) ? new Set() : new Set(files.map((f) => f.id))
            )
          }
          onRemap={openRemap}
        />
      )}
      <div className="mt-4 flex justify-between text-sm">
        <button type="button" disabled={offset === 0} onClick={() => turnPage(offset - PAGE_SIZE)}>
          Previous
        </button>
        <button
          type="button"
          disabled={offset + PAGE_SIZE >= data.total}
          onClick={() => turnPage(offset + PAGE_SIZE)}
        >
          Next
        </button>
      </div>
      {remapping && (
        <RemapPanel
          key={remapping.id}
          file={remapping}
          pending={remap.isPending}
          error={remap.error?.message ?? null}
          onCancel={() => openRemap(null)}
          onRemap={(targetFileId) =>
            remap.mutate(
              { missingId: remapping.id, targetFileId },
              {
                onSuccess: () => {
                  // The remapped row is gone, so it must not linger in the selection.
                  setSelected((s) => (s.has(remapping.id) ? toggled(s, remapping.id) : s));
                  setRemapping(null);
                },
              }
            )
          }
        />
      )}
    </div>
  );
}
