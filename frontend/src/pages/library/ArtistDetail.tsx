import { Link, Navigate, useLocation, useParams } from "react-router-dom";
import { ArrowLeft, Crown, Music } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { useArtistDetail } from "@/api/artists";
import { useDismissListed } from "@/api/versions";
import {
  MergedMembers,
  MergePicker,
  type MergedFromState,
} from "@/components/domain/library/ArtistMerge";
import { FeaturedReleasesSection } from "@/components/domain/library/FeaturedReleasesSection";
import { VersionBadges } from "@/components/domain/works/VersionBadges";

export function ArtistDetail() {
  const { artist_id } = useParams<{ artist_id: string }>();
  const { data, isLoading, isError } = useArtistDetail(artist_id);
  const mergedFrom = (useLocation().state as MergedFromState | null)?.mergedFrom;
  const dismissListed = useDismissListed();

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

  // A merged artist's page is its primary's (phase 2a S11).
  if (data.merged_into) {
    return (
      <Navigate
        to={`/library/artists/${data.merged_into}`}
        replace
        state={{ mergedFrom: { id: data.id, name: data.name } } satisfies MergedFromState}
      />
    );
  }

  const description = [data.sort_name !== data.name ? data.sort_name : null, data.disambiguation]
    .filter(Boolean)
    .join(" — ");

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

      {mergedFrom && data.members.some((member) => member.id === mergedFrom.id) && (
        <p role="status" className="mb-4 rounded-md bg-indigo-50 p-3 text-sm text-indigo-800">
          {mergedFrom.name} was merged into {data.name}, so its page is this one.
        </p>
      )}

      <PageHeader title={data.name} description={description || undefined} />

      {(data.new_version_count ?? 0) > 0 && (
        <div className="mb-4 flex items-center gap-3 rounded-md bg-indigo-50 p-3 text-sm text-indigo-800">
          <span>
            {data.new_version_count} new {data.new_version_count === 1 ? "version" : "versions"}
          </span>
          <button
            type="button"
            disabled={dismissListed.isPending}
            onClick={() => {
              const count = data.new_version_count ?? 0;
              const prompt = `Dismiss every unmapped version of ${data.name} (${count} new)? Their lines keep the master.`;
              if (window.confirm(prompt)) dismissListed.mutate({ artist_id: data.id });
            }}
          >
            Dismiss all
          </button>
        </div>
      )}

      <MergePicker artist={data} />
      <MergedMembers artist={data} />

      {/* Works list */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">Works</h2>

        {data.works.length === 0 ? (
          <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-gray-200 py-12 text-center">
            <Music className="h-8 w-8 text-gray-300" />
            <p className="text-sm text-gray-400">No works found for this artist.</p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
            {data.works.map((work, idx) => (
              <Link
                key={work.id}
                to={`/library/artists/${artist_id}/works/${work.id}`}
                className={`flex items-center justify-between px-4 py-3 hover:bg-gray-50 transition-colors ${
                  idx < data.works.length - 1 ? "border-b border-gray-100" : ""
                }`}
              >
                <div className="flex min-w-0 items-center gap-2">
                  {work.has_master && (
                    <Crown className="h-4 w-4 shrink-0 text-amber-400" aria-label="Has master" />
                  )}
                  <span className="truncate text-sm font-medium text-gray-900">{work.title}</span>
                  <VersionBadges versionTypes={work.version_types} />
                  {(work.new_version_count ?? 0) > 0 && (
                    <span className="text-xs font-medium text-indigo-600">
                      {work.new_version_count} new
                    </span>
                  )}
                </div>
                <span className="ml-4 shrink-0 text-xs text-gray-400">
                  {work.recording_count} {work.recording_count === 1 ? "recording" : "recordings"}
                </span>
              </Link>
            ))}
          </div>
        )}
      </section>

      <FeaturedReleasesSection artistId={artist_id ?? ""} />
    </div>
  );
}
