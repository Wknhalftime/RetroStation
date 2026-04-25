import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { EmptyState } from "@/components/ui/EmptyState";
import { Spinner } from "@/components/ui/Spinner";
import { MatchStatusBadge } from "@/components/ui/Badge";
import { cn } from "@/lib/utils";
import {
  useMatchingQueue,
  useRerunMatching,
  useResolveArtist,
  useResolveIdentity,
} from "@/api/matcher";
import { ArtistPanel } from "@/components/domain/matcher/ArtistPanel";
import { TitlePanel } from "@/components/domain/matcher/TitlePanel";
import { SearchSlideOver } from "@/components/domain/matcher/SearchSlideOver";
import type { MbArtistResult, QueueArtist } from "@/lib/schemas/matcher";
import { firstCandidateMbid } from "@/lib/matcher/candidates";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LibraryFile {
  id: string;
  path: string;
  title?: string | null;
}

// ---------------------------------------------------------------------------
// MatcherBrowser
// ---------------------------------------------------------------------------

const PAGE_SIZE = 25;

export function MatcherBrowser() {
  const [page, setPage] = useState(1);

  // Switching pages can scroll the selected artist off-screen (the right-side
  // panels would otherwise keep acting on an artist not shown in the list).
  // Centralize page changes through this helper so the selection clears.
  // Note: calling setState inside a setState updater is a React anti-pattern
  // that can cause unexpected behaviour in concurrent mode — use two separate
  // state updates here instead.
  const goToPage = (newPage: number) => {
    setPage(newPage);
    setSelectedArtist(null);
  };
  const offset = (page - 1) * PAGE_SIZE;
  const { data, isLoading, isError, isPlaceholderData } = useMatchingQueue(PAGE_SIZE, offset);
  const rerunMatching = useRerunMatching();
  const resolveIdentity = useResolveIdentity();
  const resolveArtist = useResolveArtist();

  const [selectedArtist, setSelectedArtist] = useState<QueueArtist | null>(null);
  const [slideOverOpen, setSlideOverOpen] = useState(false);
  const [activeIdentityId, setActiveIdentityId] = useState<string | null>(null);
  const [mbSearchOpen, setMbSearchOpen] = useState(false);
  // Captures the artist ID at the moment the curator opens MB search.
  // Without this, switching the queue selection while the slide-over is open
  // would apply the chosen MB artist to a DIFFERENT queue artist than the
  // one the search was initiated for — silent data corruption.
  const [mbSearchTargetArtistId, setMbSearchTargetArtistId] = useState<string | null>(null);

  const artists: QueueArtist[] = data?.items ?? [];
  const total: number = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // Clamp page when `total` shrinks (e.g. after resolving items reduces the queue).
  // Without this a curator on page 3 would fetch offset=50 and see items=[] even
  // though total > 0, wrongly showing the "queue empty" UI.
  //
  // Guards — only clamp when ALL of these hold:
  //   1. data != null  — real or placeholder data is available (not a blank loading state)
  //   2. total > 0     — the response has real rows; total=0 signals an empty fetch, not shrinkage
  //   3. !isPlaceholderData — we are seeing ACTUAL data for the current page, not the stale
  //                           placeholder from the previous page.  During a page transition
  //                           keepPreviousData shows the PREVIOUS page's total, so totalPages
  //                           reflects THAT page — if we clamped now we'd bounce the user back
  //                           before the new page ever loads.
  //   4. page > totalPages — the current page genuinely exceeds the new last page
  useEffect(() => {
    if (data != null && total > 0 && !isPlaceholderData && page > totalPages) {
      setPage(totalPages);
      setSelectedArtist(null);
    }
  }, [page, totalPages, data, total, isPlaceholderData]);

  function handleFileSearch(identityId: string) {
    setActiveIdentityId(identityId);
    setSlideOverOpen(true);
  }

  function handleFileSelect(file: LibraryFile) {
    if (!activeIdentityId) return;
    resolveIdentity.mutate({
      id: activeIdentityId,
      resolution: { match_status: "manual_matched", library_file_id: file.id },
    });
    setActiveIdentityId(null);
  }

  function handleOpenMbSearch() {
    if (!selectedArtist) return;
    setMbSearchTargetArtistId(selectedArtist.id);
    setMbSearchOpen(true);
  }

  function handleCloseMbSearch() {
    setMbSearchOpen(false);
    setMbSearchTargetArtistId(null);
  }

  function handleMbArtistSelect(mb: MbArtistResult) {
    // Use the artist ID captured at search-open time, not the current
    // `selectedArtist`. The curator may have navigated to a different
    // queue artist while the slide-over was open.
    if (!mbSearchTargetArtistId) return;
    resolveArtist.mutate({
      id: mbSearchTargetArtistId,
      resolution: { match_status: "manual_matched", target_artist_id: mb.id },
    });
    handleCloseMbSearch();
  }

  function handleRerun() {
    rerunMatching.mutate(undefined, {
      onSuccess: () => {
        // Re-run can reshuffle the queue — clear the selection so the right
        // panels don't act on a stale artist that may no longer exist.
        setPage(1);
        setSelectedArtist(null);
      },
    });
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Resolution Center"
        description="Link broadcast log names to MusicBrainz artists and local library files."
        actions={
          <button
            onClick={handleRerun}
            disabled={rerunMatching.isPending}
            className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            <RefreshCw className="h-4 w-4" />
            Re-run Matching
          </button>
        }
      />

      {isLoading && (
        <div className="flex flex-1 items-center justify-center">
          <Spinner className="h-8 w-8 text-gray-400" />
        </div>
      )}

      {isError && !isLoading && (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-red-500">Failed to load matching queue.</p>
        </div>
      )}

      {!isLoading && !isError && total === 0 && (
        <div className="flex flex-1 items-center justify-center">
          <EmptyState
            title="Queue is empty"
            description="All artists have been resolved or the queue has not been populated yet."
          />
        </div>
      )}

      {!isLoading && !isError && total > 0 && (
        <div className="flex min-h-0 flex-1 gap-4">
          {/* Left — artist list */}
          <aside className="flex w-80 shrink-0 flex-col overflow-hidden rounded-lg border border-gray-200 bg-white">
            <div className="border-b border-gray-100 px-4 py-2">
              <p className="text-xs font-medium uppercase tracking-wide text-gray-400">
                Artists ({artists.length})
              </p>
            </div>
            <ul className="flex-1 overflow-y-auto">
              {artists.map((artist) => (
                <li key={artist.id}>
                  <button
                    onClick={() => setSelectedArtist(artist)}
                    className={cn(
                      "flex w-full items-center justify-between gap-2 px-4 py-3 text-left hover:bg-gray-50",
                      selectedArtist?.id === artist.id && "bg-blue-50"
                    )}
                  >
                    <span className="min-w-0 truncate text-sm font-medium text-gray-800">
                      {artist.original_name}
                    </span>
                    <MatchStatusBadge status={artist.match_status} className="shrink-0" />
                  </button>
                </li>
              ))}
            </ul>
            <div className="flex items-center justify-between border-t border-gray-100 px-4 py-2">
              <button
                onClick={() => goToPage(Math.max(1, page - 1))}
                disabled={page === 1}
                className="text-xs text-gray-500 disabled:opacity-40"
              >
                ← Prev
              </button>
              <span className="text-xs text-gray-400">
                Page {page} of {totalPages} ({total} total)
              </span>
              <button
                onClick={() => goToPage(Math.min(totalPages, page + 1))}
                disabled={page === totalPages}
                className="text-xs text-gray-500 disabled:opacity-40"
              >
                Next →
              </button>
            </div>
          </aside>

          {/* Right — panels */}
          <div className="flex min-w-0 flex-1 flex-col gap-4">
            {selectedArtist ? (
              <>
                <ArtistPanel artist={selectedArtist} onSearchMusicBrainz={handleOpenMbSearch} />
                <TitlePanel artist={selectedArtist} onFileSearch={handleFileSearch} />
              </>
            ) : (
              <div className="flex flex-1 items-center justify-center rounded-lg border border-dashed border-gray-300 p-12 text-center">
                <p className="text-sm text-gray-400">Select an artist from the list to begin.</p>
              </div>
            )}
          </div>
        </div>
      )}

      <SearchSlideOver
        open={slideOverOpen}
        onClose={() => setSlideOverOpen(false)}
        mode="file"
        restrictArtistMbid={firstCandidateMbid(selectedArtist)}
        onSelectFile={handleFileSelect}
      />

      <SearchSlideOver
        open={mbSearchOpen}
        onClose={handleCloseMbSearch}
        mode="mb-artist"
        onSelectMbArtist={handleMbArtistSelect}
      />
    </div>
  );
}
