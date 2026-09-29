import { useEffect, useState } from "react";
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
import { PurgeMissingSetting } from "@/components/domain/library/PurgeMissingSetting";
import { RemapPanel } from "@/components/domain/library/RemapPanel";
import { deletionPrompt } from "@/components/domain/library/deletionPrompt";
import type { MissingFile, MissingFileDeletion } from "@/lib/schemas/missing";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;
const ACTION = cn(
  "rounded-md bg-red-600 px-3 py-2 text-sm font-medium text-white",
  "hover:bg-red-700 disabled:opacity-50"
);

type DeleteRequest = (body: DeleteMissingFilesBody, count: number, matches: number) => void;

function toggled(set: ReadonlySet<string>, id: string): ReadonlySet<string> {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

function matchSum(files: MissingFile[]): number {
  return files.reduce((sum, f) => sum + f.match_count, 0);
}

function matchesReleased(matches: number): string {
  return matches === 1 ? "1 match released" : `${matches} matches released`;
}

/** Where an emptied page steps back to: the last page that still has rows, never forward. */
function steppedBack(offset: number, total: number): number {
  const lastPage = total === 0 ? 0 : Math.floor((total - 1) / PAGE_SIZE) * PAGE_SIZE;
  return Math.max(0, Math.min(offset - PAGE_SIZE, lastPage));
}

function DeletionNote({ result }: { result: MissingFileDeletion | undefined }) {
  if (!result) return null;
  return (
    <p role="status" className="mb-4 rounded-md bg-green-50 p-3 text-sm text-green-800">
      Deleted {result.deleted}; {matchesReleased(result.matches_released)}
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

function DeleteActions({
  chosen,
  total,
  totalMatches,
  busy,
  onDelete,
}: {
  chosen: MissingFile[];
  total: number;
  totalMatches: number;
  busy: boolean;
  onDelete: DeleteRequest;
}) {
  return (
    <>
      <button
        type="button"
        className={ACTION}
        disabled={chosen.length === 0 || busy}
        onClick={() => onDelete({ ids: chosen.map((f) => f.id) }, chosen.length, matchSum(chosen))}
      >
        Delete selected
      </button>
      <button
        type="button"
        className={ACTION}
        disabled={total === 0 || busy}
        onClick={() => onDelete({ all: true }, total, totalMatches)}
      >
        Delete all
      </button>
    </>
  );
}

function Pager({
  offset,
  total,
  onTurn,
}: {
  offset: number;
  total: number;
  onTurn: (offset: number) => void;
}) {
  return (
    <div className="mt-4 flex justify-between text-sm">
      <button type="button" disabled={offset === 0} onClick={() => onTurn(offset - PAGE_SIZE)}>
        Previous
      </button>
      <button
        type="button"
        disabled={offset + PAGE_SIZE >= total}
        onClick={() => onTurn(offset + PAGE_SIZE)}
      >
        Next
      </button>
    </div>
  );
}

export function MissingFiles() {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [remapping, setRemapping] = useState<MissingFile | null>(null);
  const { data, isLoading, isError } = useMissingFiles(offset, PAGE_SIZE);
  const deletion = useDeleteMissingFiles();
  const remap = useRemapMissingFile();

  // Deleting or remapping every row of a later page leaves it empty: step back to one with rows.
  const pageEmptied = !!data && data.items.length === 0 && offset > 0;
  const total = data?.total ?? 0;
  useEffect(() => {
    if (pageEmptied) setOffset(steppedBack(offset, total));
  }, [pageEmptied, offset, total]);

  if (isLoading || pageEmptied)
    return <Spinner className="mx-auto my-16 h-8 w-8 text-indigo-500" />;
  if (isError || !data)
    return <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">Failed to load.</p>;

  const files = data.items;
  // Only rows still on screen count: a refetch may have removed a selected one.
  const chosen = files.filter((f) => selected.has(f.id));
  const confirmThenDelete: DeleteRequest = (body, count, matches) => {
    if (!window.confirm(deletionPrompt(count, matches))) return;
    deletion.mutate(body, { onSuccess: () => setSelected(new Set()) });
  };
  const turnPage = (next: number) => {
    setSelected(new Set());
    deletion.reset();
    setOffset(next);
  };
  // A refusal belongs to the row it was raised for; opening or closing a panel clears it,
  // unless a remap is still in flight.
  const openRemap = (file: MissingFile | null) => {
    if (!remap.isPending) remap.reset();
    setRemapping(file);
  };

  return (
    <div>
      <PageHeader
        title="Missing Files"
        description={`${data.total} indexed files are no longer on disk.`}
        actions={
          <DeleteActions
            chosen={chosen}
            total={data.total}
            totalMatches={data.total_match_count}
            busy={deletion.isPending}
            onDelete={confirmThenDelete}
          />
        }
      />
      <div className="mb-4">
        <PurgeMissingSetting />
      </div>
      <DeletionNote result={deletion.data} />
      <DeletionError error={deletion.error} />
      {data.total === 0 ? (
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
          remapDisabled={remap.isPending}
        />
      )}
      <Pager offset={offset} total={data.total} onTurn={turnPage} />
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
              { onSuccess: () => setRemapping(null) }
            )
          }
        />
      )}
    </div>
  );
}
