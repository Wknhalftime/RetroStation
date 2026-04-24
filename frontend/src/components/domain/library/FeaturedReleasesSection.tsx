import { Disc } from "lucide-react";

interface FeaturedReleasesSectionProps {
  artistId: string;
}

export function FeaturedReleasesSection({ artistId: _artistId }: FeaturedReleasesSectionProps) {
  return (
    <section className="mt-6">
      <div className="mb-3 flex items-center gap-2">
        <Disc className="h-4 w-4 text-gray-400" />
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">
          Featured Appearances
        </h2>
      </div>
      <div className="rounded-xl border border-dashed border-gray-200 bg-gray-50 px-5 py-8 text-center">
        <p className="text-sm text-gray-500">
          Featured release detection requires enrichment data. Once this artist's files are
          enriched, featured appearances will appear here automatically.
        </p>
      </div>
    </section>
  );
}
