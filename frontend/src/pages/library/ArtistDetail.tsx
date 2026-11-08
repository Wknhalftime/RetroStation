import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Combine, Crown, Music } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { useArtistDetail } from "@/api/artists";
import { useMergeWorks, type MergeWorksResult } from "@/api/works";
import { FeaturedReleasesSection } from "@/components/domain/library/FeaturedReleasesSection";
import { VersionBadges } from "@/components/domain/works/VersionBadges";
import { errorText } from "@/lib/errors";
import type { WorkSummary } from "@/lib/schemas/artists";

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** The work a merge keeps: the most recordings, the first in the list on a tie. */
function pickKeeper(works: WorkSummary[]): WorkSummary {
  return works.reduce((best, w) => (w.recording_count > best.recording_count ? w : best));
}

/** What a finished merge did, in one line. */
function mergedText(result: MergeWorksResult, keeperTitle: string): string {
  const parts = [
    `Merged ${plural(result.deleted_work_count, "work")} into “${keeperTitle}”: ` +
      `${plural(result.merged_file_count, "file")} moved.`,
  ];
  if (result.dropped_override_count > 0) {
    parts.push(
      `${plural(result.dropped_override_count, "format override")} dropped ` +
        "(the kept work already had one for the same format)."
    );
  }
  return parts.join(" ");
}

export function ArtistDetail() {
  const { artist_id } = useParams<{ artist_id: string }>();
  const { data, isLoading, isError } = useArtistDetail(artist_id);
  const mergeWorks = useMergeWorks();

  const [picked, setPicked] = useState<string[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [keptTitle, setKeptTitle] = useState("");

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
        Failed to load artist. Please try again.
      </p>
    );
  }

  const description = [data.sort_name !== data.name ? data.sort_name : null, data.disambiguation]
    .filter(Boolean)
    .join(" — ");

  // In list order, so a tie on recordings keeps the work listed first.
  const pickedWorks = data.works.filter((w) => picked.includes(w.id));
  const keeper = pickedWorks.length >= 2 ? pickKeeper(pickedWorks) : null;

  const toggle = (workId: string) => {
    setConfirming(false);
    setPicked((prev) =>
      prev.includes(workId) ? prev.filter((id) => id !== workId) : [...prev, workId]
    );
  };
  const clearPicks = () => {
    setConfirming(false);
    setPicked([]);
  };
  const askToMerge = () => {
    mergeWorks.reset();
    setConfirming(true);
  };
  const runMerge = () => {
    if (!keeper) return;
    const sourceWorkIds = pickedWorks.filter((w) => w.id !== keeper.id).map((w) => w.id);
    setKeptTitle(keeper.title);
    mergeWorks.mutate(
      { targetId: keeper.id, sourceWorkIds },
      { onSuccess: clearPicks, onError: () => setConfirming(false) }
    );
  };

  return (
    <div>
      {/* Back link */}
      <div className="mb-4">
        <Link
          to="/library/artists"
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          All Artists
        </Link>
      </div>

      <PageHeader title={data.name} description={description || undefined} />

      {/* Works list */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">Works</h2>

        {/* What the last merge did, or why it failed */}
        {mergeWorks.data && (
          <p
            role="status"
            className="mb-3 rounded-md bg-green-50 px-4 py-2.5 text-sm text-green-800"
          >
            {mergedText(mergeWorks.data, keptTitle)}
          </p>
        )}
        {mergeWorks.error && (
          <p role="alert" className="mb-3 rounded-md bg-red-50 px-4 py-2.5 text-sm text-red-700">
            Could not merge: {errorText(mergeWorks.error)}
          </p>
        )}

        {/* Merge bar: always there (fixed height), so ticking never shifts the list */}
        {data.works.length >= 2 && (
          <div className="mb-3 flex min-h-14 flex-wrap items-center gap-3 rounded-lg border border-indigo-200 bg-indigo-50 px-4 py-2.5 text-sm text-indigo-900">
            {!keeper ? (
              <>
                <span className="min-w-0 flex-1 text-indigo-700">
                  {picked.length === 1
                    ? "1 selected: tick another work that is the same song."
                    : "Tick two or more works that are the same song to merge them."}
                </span>
                {picked.length === 1 && (
                  <button
                    type="button"
                    onClick={clearPicks}
                    className="rounded-md border border-gray-300 bg-white px-3 py-1.5 font-medium text-gray-700 hover:bg-gray-50"
                  >
                    Clear
                  </button>
                )}
              </>
            ) : confirming ? (
              <>
                <span className="min-w-0 flex-1">
                  Keep <span className="font-medium">“{keeper.title}”</span> and fold{" "}
                  {plural(pickedWorks.length - 1, "other work")} into it? The others are deleted and
                  the master file is picked again.
                </span>
                <button
                  type="button"
                  onClick={runMerge}
                  disabled={mergeWorks.isPending}
                  className="rounded-md bg-red-600 px-3 py-1.5 font-medium text-white hover:bg-red-700 disabled:opacity-50"
                >
                  {mergeWorks.isPending ? "Merging..." : "Confirm merge"}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirming(false)}
                  className="rounded-md border border-gray-300 bg-white px-3 py-1.5 font-medium text-gray-700 hover:bg-gray-50"
                >
                  Cancel
                </button>
              </>
            ) : (
              <>
                <span className="min-w-0 flex-1">{pickedWorks.length} selected</span>
                <button
                  type="button"
                  onClick={askToMerge}
                  className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 font-medium text-white hover:bg-indigo-700"
                >
                  <Combine className="h-4 w-4" />
                  {`Merge ${plural(pickedWorks.length, "work")}`}
                </button>
                <button
                  type="button"
                  onClick={clearPicks}
                  className="rounded-md border border-gray-300 bg-white px-3 py-1.5 font-medium text-gray-700 hover:bg-gray-50"
                >
                  Clear
                </button>
              </>
            )}
          </div>
        )}

        {data.works.length === 0 ? (
          <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-gray-200 py-12 text-center">
            <Music className="h-8 w-8 text-gray-300" />
            <p className="text-sm text-gray-400">No works found for this artist.</p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
            {data.works.map((work, idx) => (
              <div
                key={work.id}
                className={`flex items-center hover:bg-gray-50 transition-colors ${
                  idx < data.works.length - 1 ? "border-b border-gray-100" : ""
                } ${picked.includes(work.id) ? "bg-indigo-50/60" : ""}`}
              >
                {/* Outside the link, so ticking a box never opens the work */}
                <label className="flex cursor-pointer items-center self-stretch pl-4 pr-1">
                  <input
                    type="checkbox"
                    aria-label={`Select ${work.title}`}
                    checked={picked.includes(work.id)}
                    onChange={() => toggle(work.id)}
                    className="h-4 w-4 rounded border-gray-300 text-indigo-600"
                  />
                </label>
                <Link
                  to={`/library/artists/${artist_id}/works/${work.id}`}
                  className="flex min-w-0 flex-1 items-center justify-between py-3 pl-2 pr-4"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    {work.has_master && (
                      <Crown className="h-4 w-4 shrink-0 text-amber-400" aria-label="Has master" />
                    )}
                    <span className="truncate text-sm font-medium text-gray-900">{work.title}</span>
                    <VersionBadges versionTypes={work.version_types} />
                  </div>
                  <span className="ml-4 shrink-0 text-xs text-gray-400">
                    {plural(work.recording_count, "recording")}
                  </span>
                </Link>
              </div>
            ))}
          </div>
        )}
      </section>

      <FeaturedReleasesSection artistId={artist_id ?? ""} />
    </div>
  );
}
