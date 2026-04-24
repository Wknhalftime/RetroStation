import { useRef, useState, useEffect, useCallback } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Search } from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { useArtistsInfinite } from "@/api/artists";
import type { ArtistSummary } from "@/lib/schemas/artists";

const ROW_HEIGHT = 56;

// ---------------------------------------------------------------------------
// Debounce hook
// ---------------------------------------------------------------------------

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState<T>(value);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(id);
  }, [value, delay]);
  return debounced;
}

// ---------------------------------------------------------------------------
// ArtistRow
// ---------------------------------------------------------------------------

function ArtistRow({ artist }: { artist: ArtistSummary }) {
  return (
    <Link
      to={`/library/artists/${artist.id}`}
      className="flex items-center justify-between px-4 hover:bg-gray-50 transition-colors border-b border-gray-100"
      style={{ height: ROW_HEIGHT }}
    >
      <div className="min-w-0">
        <span className="block truncate text-sm font-medium text-gray-900">{artist.name}</span>
        {artist.disambiguation && (
          <span className="block truncate text-xs text-gray-400">{artist.disambiguation}</span>
        )}
      </div>
      <div className="ml-4 flex shrink-0 items-center gap-4 text-xs text-gray-500">
        <span>
          <span className="font-medium text-gray-700">{artist.work_count}</span>{" "}
          {artist.work_count === 1 ? "work" : "works"}
        </span>
        <span>
          <span className="font-medium text-gray-700">{artist.file_count}</span>{" "}
          {artist.file_count === 1 ? "file" : "files"}
        </span>
      </div>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// ArtistBrowser
// ---------------------------------------------------------------------------

export function ArtistBrowser() {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 300);

  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage, isError } =
    useArtistsInfinite(debouncedSearch || undefined);

  // Flatten pages into a single array
  const allArtists: ArtistSummary[] = data?.pages.flatMap((p) => p.items) ?? [];

  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: allArtists.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10,
  });

  const virtualItems = virtualizer.getVirtualItems();

  // Load next page when within 5 items of the bottom
  const loadMore = useCallback(() => {
    if (hasNextPage && !isFetchingNextPage) {
      void fetchNextPage();
    }
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  useEffect(() => {
    if (virtualItems.length === 0) return;
    const lastItem = virtualItems[virtualItems.length - 1];
    if (lastItem.index >= allArtists.length - 5) {
      loadMore();
    }
  }, [virtualItems, allArtists.length, loadMore]);

  return (
    <div>
      <PageHeader
        title="Artists"
        description="Browse all artists in your music library."
        actions={
          <Link
            to="/library"
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            <ArrowLeft className="h-4 w-4" />
            Library
          </Link>
        }
      />

      {/* Search input */}
      <div className="mb-4 relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search artists…"
          className="w-full rounded-md border border-gray-300 bg-white py-2 pl-9 pr-4 text-sm text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
      </div>

      {/* Loading initial */}
      {isLoading && (
        <div className="flex justify-center py-16">
          <Spinner className="h-8 w-8 text-indigo-500" />
        </div>
      )}

      {/* Error */}
      {isError && (
        <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
          Failed to load artists. Please try again.
        </p>
      )}

      {/* Empty */}
      {!isLoading && !isError && allArtists.length === 0 && (
        <p className="py-12 text-center text-sm text-gray-400">
          {debouncedSearch ? `No artists matching "${debouncedSearch}".` : "No artists found."}
        </p>
      )}

      {/* Virtual list */}
      {allArtists.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
          <div
            ref={parentRef}
            className="overflow-auto"
            style={{ maxHeight: "calc(100vh - 280px)" }}
          >
            <div
              style={{
                height: virtualizer.getTotalSize(),
                width: "100%",
                position: "relative",
              }}
            >
              {virtualItems.map((virtualItem) => {
                const artist = allArtists[virtualItem.index];
                return (
                  <div
                    key={virtualItem.key}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      height: `${virtualItem.size}px`,
                      transform: `translateY(${virtualItem.start}px)`,
                    }}
                  >
                    <ArtistRow artist={artist} />
                  </div>
                );
              })}
            </div>
          </div>

          {/* Spinner at bottom when loading next page */}
          {isFetchingNextPage && (
            <div className="flex justify-center border-t border-gray-100 py-3">
              <Spinner className="h-5 w-5 text-indigo-400" />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
