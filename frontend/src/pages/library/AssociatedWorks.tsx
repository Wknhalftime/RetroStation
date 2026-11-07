import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Combine, RotateCcw } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { WorkFilesTable } from "@/components/domain/works/WorkFilesTable";
import { FormatOverridePanel } from "@/components/domain/works/FormatOverridePanel";
import { MergeWorksDialog } from "@/components/domain/works/MergeWorksDialog";
import {
  useWorkDetail,
  useSetMaster,
  useRevertMaster,
  useCreateFormatOverride,
  useDeleteFormatOverride,
  useMergeWorks,
  type MergeWorksResult,
} from "@/api/works";
import { useArtistDetail } from "@/api/artists";
import { ValidationError } from "@/api/client";

/** The text to show for a refused master change: the server's reason where it gave one. */
function errorText(error: Error): string {
  if (error instanceof ValidationError) {
    // FastAPI sends a list of {msg}; a hand-raised 422 sends a plain string.
    const detail: unknown = error.detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail)) {
      const reasons = detail
        .map((item: unknown) =>
          typeof item === "object" && item !== null && "msg" in item ? String(item.msg) : ""
        )
        .filter(Boolean);
      if (reasons.length > 0) return reasons.join(" ");
    }
  }
  return error.message;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** What a finished merge did, in one line. */
function mergedText(result: MergeWorksResult): string {
  const parts = [
    `Merged ${plural(result.deleted_work_count, "work")} into this one: ` +
      `${plural(result.merged_file_count, "file")} moved.`,
  ];
  if (result.dropped_override_count > 0) {
    parts.push(
      `${plural(result.dropped_override_count, "format override")} dropped ` +
        "(this work already had one for the same format)."
    );
  }
  return parts.join(" ");
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function AssociatedWorks() {
  const { artist_id, work_id } = useParams<{
    artist_id: string;
    work_id: string;
  }>();

  const { data: work, isLoading: workLoading, isError: workError } = useWorkDetail(work_id);

  const { data: artist } = useArtistDetail(artist_id);

  const setMaster = useSetMaster(work_id ?? "");
  const revertMaster = useRevertMaster(work_id ?? "");
  const createOverride = useCreateFormatOverride(work_id ?? "");
  const deleteOverride = useDeleteFormatOverride(work_id ?? "");
  const mergeWorks = useMergeWorks(work_id ?? "");

  const [mergeOpen, setMergeOpen] = useState(false);
  // A fresh dialog (no picks, no filter) each time it opens.
  const [mergeSession, setMergeSession] = useState(0);

  // ---------------------------------------------------------------------------
  // Loading / error states
  // ---------------------------------------------------------------------------

  if (workLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }

  if (workError || !work) {
    return (
      <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
        Failed to load work details. Please try again.
      </p>
    );
  }

  // ---------------------------------------------------------------------------
  // Derived values
  // ---------------------------------------------------------------------------

  const masterFileId = work.song_master?.preferred_file_id ?? null;
  const masterError = setMaster.error ?? revertMaster.error;

  const chooseMaster = (fileId: string) => {
    revertMaster.reset();
    setMaster.mutate({ preferred_file_id: fileId });
  };
  const revertToAuto = () => {
    setMaster.reset();
    revertMaster.mutate();
  };
  const masterMethod = work.song_master?.selection_method ?? null;

  const mergeCandidates = (artist?.works ?? []).filter((w) => w.id !== work.id);
  const openMerge = () => {
    mergeWorks.reset();
    setMergeSession((n) => n + 1);
    setMergeOpen(true);
  };
  const runMerge = (sourceWorkIds: string[]) => {
    mergeWorks.mutate(sourceWorkIds, { onSuccess: () => setMergeOpen(false) });
  };

  const masterBannerText = work.song_master
    ? [
        `Selection: ${work.song_master.selection_method}`,
        work.song_master.score != null
          ? `Score: ${Math.round(work.song_master.score * 100)}%`
          : null,
      ]
        .filter(Boolean)
        .join("  ·  ")
    : null;

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Back link */}
      <div>
        <Link
          to={`/library/artists/${artist_id}`}
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          {artist?.name ?? "Artist"}
        </Link>
      </div>

      {/* Page header */}
      <PageHeader
        title={work.title}
        description={artist?.name}
        actions={
          <>
            <button
              type="button"
              onClick={openMerge}
              className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
            >
              <Combine className="h-4 w-4" />
              Merge duplicates
            </button>
            {work.song_master && (
              <button
                type="button"
                onClick={revertToAuto}
                disabled={revertMaster.isPending}
                className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60"
              >
                <RotateCcw className="h-4 w-4" />
                {revertMaster.isPending ? "Reverting..." : "Revert to Auto"}
              </button>
            )}
          </>
        }
      />

      <MergeWorksDialog
        key={mergeSession}
        open={mergeOpen}
        onClose={() => setMergeOpen(false)}
        keepTitle={work.title}
        candidates={mergeCandidates}
        onMerge={runMerge}
        isMerging={mergeWorks.isPending}
        error={mergeWorks.error ? errorText(mergeWorks.error) : null}
      />

      {/* What the last merge did */}
      {mergeWorks.data && (
        <p role="status" className="rounded-md bg-green-50 px-4 py-2.5 text-sm text-green-800">
          {mergedText(mergeWorks.data)}
        </p>
      )}

      {/* Master info banner */}
      {masterBannerText && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800">
          <span className="font-medium">Master file: </span>
          {masterBannerText}
        </div>
      )}

      {/* Why the last master change failed */}
      {masterError && (
        <p role="alert" className="rounded-md bg-red-50 px-4 py-2.5 text-sm text-red-700">
          Could not change the master file: {errorText(masterError)}
        </p>
      )}

      {/* Files table */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">
          Recordings &amp; Files
        </h2>
        <WorkFilesTable
          recordings={work.recordings}
          masterFileId={masterFileId}
          masterMethod={masterMethod}
          onSetMaster={chooseMaster}
        />
      </section>

      {/* Format overrides */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">
          Format Overrides
        </h2>
        <FormatOverridePanel
          overrides={work.format_overrides}
          recordings={work.recordings}
          onAdd={(data) => createOverride.mutate(data)}
          onDelete={(oid) => deleteOverride.mutate({ oid })}
          isAdding={createOverride.isPending}
          isDeleting={deleteOverride.isPending}
        />
      </section>
    </div>
  );
}
