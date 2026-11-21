import { useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { ArrowLeft, RotateCcw } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { CopyRow } from "@/components/domain/works/CopyRow";
import { PerformanceList } from "@/components/domain/works/PerformanceList";
import { SetMasterDialog } from "@/components/domain/works/SetMasterDialog";
import { TwoActsNotice } from "@/components/domain/works/TwoActsNotice";
import { FormatOverridePanel } from "@/components/domain/works/FormatOverridePanel";
import { WorkVersionsPanel } from "@/components/domain/works/WorkVersionsPanel";
import {
  useWorkDetail,
  useSetMaster,
  useRevertMaster,
  useCreateFormatOverride,
  useDeleteFormatOverride,
  usePreferCopy,
  useReleasePins,
} from "@/api/works";
import { useArtistDetail } from "@/api/artists";
import type { FileInfo } from "@/lib/schemas/works";
import { errorText } from "@/lib/errors";

// The work page (phase 3b PR 5a; SPEC S8, D41): the work's performances, each with its credit
// and copies, in place of "Recordings & Files"; Set master on each copy, asking first about the
// pins of the copy it replaces (C72); Prefer this copy; pinned lines with Release; the
// two-acts notice. At /library/works/:work_id its back link is the Library; at an artist's
// address, that artist.

const SECTION_HEADING = "mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500";

function CopySection({
  label,
  files,
  masterFileId,
  isManual,
  onSetMaster,
}: {
  label: string;
  files: FileInfo[];
  masterFileId: string | null;
  isManual: boolean;
  onSetMaster: (fileId: string) => void;
}) {
  if (files.length === 0) return null;
  return (
    <section
      aria-label={label}
      className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm"
    >
      <div className="bg-gray-50 px-4 py-2 text-sm font-semibold text-gray-800">{label}</div>
      <ul>
        {files.map((file) => (
          <CopyRow
            key={file.id}
            file={file}
            isMaster={file.id === masterFileId}
            isManual={isManual}
            onSetMaster={onSetMaster}
          />
        ))}
      </ul>
    </section>
  );
}

export function AssociatedWorks() {
  const { artist_id, work_id } = useParams<{ artist_id: string; work_id: string }>();

  const { data: work, isLoading: workLoading, isError: workError } = useWorkDetail(work_id);
  const { data: artist } = useArtistDetail(artist_id);

  const setMaster = useSetMaster(work_id ?? "");
  const revertMaster = useRevertMaster(work_id ?? "");
  const createOverride = useCreateFormatOverride(work_id ?? "");
  const deleteOverride = useDeleteFormatOverride(work_id ?? "");
  const preferCopy = usePreferCopy(work_id ?? "");
  const releasePins = useReleasePins(work_id ?? "");
  const [replacing, setReplacing] = useState<string | null>(null);

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

  // A merged-away id answers its merged work: the URL follows (3b S3, Q19), at either address.
  if (work.id !== work_id) {
    const to = artist_id
      ? `/library/artists/${artist_id}/works/${work.id}`
      : `/library/works/${work.id}`;
    return <Navigate to={to} replace />;
  }

  const performances = work.performances ?? [];
  const acts = work.acts ?? [];
  const masterFileId = work.song_master?.preferred_file_id ?? null;
  const isManual = work.song_master?.selection_method === "manual";
  const files = work.recordings.flatMap((r) => r.files);
  const copies = performances.flatMap((p) => p.copies);
  const inPerformance = new Set(copies.map((c) => c.file.id));
  const notYet = files.filter((f) => !f.other_work && !inPerformance.has(f.id));
  const elsewhere = files.filter((f) => f.other_work);
  const masterPins = copies.find((c) => c.file.id === masterFileId)?.pins ?? [];
  const pathOf = (fileId: string | null) =>
    [...copies.map((c) => c.file), ...files].find((f) => f.id === fileId)?.file_path ?? "";

  const resetErrors = () => {
    setMaster.reset();
    revertMaster.reset();
    preferCopy.reset();
    releasePins.reset();
  };
  const chooseMaster = (fileId: string) => {
    resetErrors();
    if (fileId !== masterFileId && masterPins.length > 0) {
      setReplacing(fileId);
      return;
    }
    setMaster.mutate({ preferred_file_id: fileId });
  };
  const confirmMaster = async (release: string[]) => {
    const chosen = replacing;
    setReplacing(null);
    if (chosen === null) return;
    if (release.length > 0) {
      try {
        await releasePins.mutateAsync(release);
      } catch {
        return; // the alert says why; no master is set
      }
    }
    setMaster.mutate({ preferred_file_id: chosen });
  };
  const revertToAuto = () => {
    resetErrors();
    revertMaster.mutate();
  };

  const masterError = setMaster.error ?? revertMaster.error;
  const masterBannerText = work.song_master
    ? [
        `Selection: ${work.song_master.selection_method}`,
        // Points (release status + release type + format), not a fraction.
        work.song_master.score != null ? `Score: ${work.song_master.score} points` : null,
      ]
        .filter(Boolean)
        .join("  ·  ")
    : null;
  const credited = (work.artists ?? []).map((a) => a.name).join(", ");
  const twoActs = acts.length >= 2;

  return (
    <div className="space-y-6">
      <div>
        <Link
          to={artist_id ? `/library/artists/${artist_id}` : "/library"}
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          {artist_id ? (artist?.name ?? "Artist") : "Library"}
        </Link>
      </div>

      <PageHeader
        title={work.title}
        description={credited || artist?.name}
        actions={
          work.song_master ? (
            <button
              type="button"
              onClick={revertToAuto}
              disabled={revertMaster.isPending}
              className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60"
            >
              <RotateCcw className="h-4 w-4" />
              {revertMaster.isPending ? "Reverting..." : "Revert to Auto"}
            </button>
          ) : undefined
        }
      />

      {masterBannerText && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800">
          <span className="font-medium">Master file: </span>
          {masterBannerText}
        </div>
      )}

      {masterError && (
        <p role="alert" className="rounded-md bg-red-50 px-4 py-2.5 text-sm text-red-700">
          Could not change the master file: {errorText(masterError)}
        </p>
      )}
      {preferCopy.error && (
        <p role="alert" className="rounded-md bg-red-50 px-4 py-2.5 text-sm text-red-700">
          Could not prefer the copy: {errorText(preferCopy.error)}
        </p>
      )}
      {releasePins.error && (
        <p role="alert" className="rounded-md bg-red-50 px-4 py-2.5 text-sm text-red-700">
          Could not release the line: {errorText(releasePins.error)}
        </p>
      )}

      {replacing !== null && (
        <SetMasterDialog
          pins={masterPins}
          oldPath={pathOf(masterFileId)}
          newPath={pathOf(replacing)}
          busy={releasePins.isPending || setMaster.isPending}
          onConfirm={(release) => void confirmMaster(release)}
          onCancel={() => setReplacing(null)}
        />
      )}

      <section className="space-y-4">
        <h2 className={SECTION_HEADING}>Performances</h2>
        <PerformanceList
          performances={performances}
          masterFileId={masterFileId}
          isManual={isManual}
          onSetMaster={chooseMaster}
          onPrefer={(performanceId, fileId) => {
            resetErrors();
            preferCopy.mutate({ performanceId, fileId });
          }}
          onRelease={(identityId) => {
            resetErrors();
            releasePins.mutate([identityId]);
          }}
          busy={preferCopy.isPending || releasePins.isPending}
        />
        <CopySection
          label="Not in a performance yet"
          files={notYet}
          masterFileId={masterFileId}
          isManual={isManual}
          onSetMaster={chooseMaster}
        />
        <CopySection
          label="On other works"
          files={elsewhere}
          masterFileId={masterFileId}
          isManual={isManual}
          onSetMaster={chooseMaster}
        />
      </section>

      <section>
        <h2 className={SECTION_HEADING}>Format Overrides</h2>
        <FormatOverridePanel
          overrides={work.format_overrides}
          recordings={work.recordings}
          onAdd={(data) => createOverride.mutate(data)}
          onDelete={(oid) => deleteOverride.mutate({ oid })}
          isAdding={createOverride.isPending}
          isDeleting={deleteOverride.isPending}
          notice={twoActs ? (format) => <TwoActsNotice acts={acts} format={format} /> : undefined}
        />
      </section>

      <section>
        <WorkVersionsPanel
          workId={work_id ?? ""}
          versions={work.versions ?? []}
          masterFlag={work.master_flag ?? null}
          recordings={work.recordings}
          notice={twoActs ? <TwoActsNotice acts={acts} /> : undefined}
        />
      </section>
    </div>
  );
}
