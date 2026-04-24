import { Link, useParams } from "react-router-dom";
import { ArrowLeft, RotateCcw } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { WorkFilesTable } from "@/components/domain/works/WorkFilesTable";
import { FormatOverridePanel } from "@/components/domain/works/FormatOverridePanel";
import {
  useWorkDetail,
  useSetMaster,
  useRevertMaster,
  useCreateFormatOverride,
  useDeleteFormatOverride,
} from "@/api/works";
import { useArtistDetail } from "@/api/artists";

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
  const masterMethod = work.song_master?.selection_method ?? null;

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
          work.song_master ? (
            <button
              type="button"
              onClick={() => revertMaster.mutate()}
              disabled={revertMaster.isPending}
              className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60"
            >
              <RotateCcw className="h-4 w-4" />
              {revertMaster.isPending ? "Reverting..." : "Revert to Auto"}
            </button>
          ) : undefined
        }
      />

      {/* Master info banner */}
      {masterBannerText && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800">
          <span className="font-medium">Master file: </span>
          {masterBannerText}
        </div>
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
          onSetMaster={(fileId) => setMaster.mutate({ preferred_file_id: fileId })}
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
