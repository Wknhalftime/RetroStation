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
  type RunMatchingResponse,
} from "@/api/matcher";
import { ConflictError } from "@/api/client";
import { ArtistPanel } from "@/components/domain/matcher/ArtistPanel";
import { TitlePanel } from "@/components/domain/matcher/TitlePanel";
import { SearchSlideOver } from "@/components/domain/matcher/SearchSlideOver";
import type { MbArtistResult, QueueArtist } from "@/lib/schemas/matcher";
import { firstCandidateMbid } from "@/lib/matcher/candidates";

type Feedback = { kind: "success" | "error"; message: string } | null;

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// Backend signals "another /matching/run is already in flight" via
// HTTPException(409, detail="matching_run_in_progress"). apiFetch maps that to
// a ConflictError whose message carries the raw detail string. Render a
// friendly toast instead of the raw enum value.
function rerunErrorMessage(err: unknown): string {
  if (err instanceof ConflictError && err.message === "matching_run_in_progress") {
    return "Matching is already running — please wait for the current run to finish.";
  }
  return errorMessage(err);
}

function rerunSuccessMessage(resp: RunMatchingResponse): string {
  const { reset, enqueue } = resp;
  const parts: string[] = [];
  if (reset.performed) {
    parts.push(
      `Reset ${reset.identities_reset} title${reset.identities_reset === 1 ? "" : "s"}` +
        (reset.artists_reset
          ? ` and ${reset.artists_reset} artist${reset.artists_reset === 1 ? "" : "s"}`
          : ""),
    );
  }
  parts.push(
    `Queued matching for ${enqueue.playlists_queued} playlist${enqueue.playlists_queued === 1 ? "" : "s"}`,
  );
  if (enqueue.enqueue_failures > 0) {
    parts.push(`(${enqueue.enqueue_failures} enqueue failure${enqueue.enqueue_failures === 1 ? "" : "s"})`);
  }
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LibraryFile {
  id: string;
  file_path: string;
  track_title?: string | null;
}

interface LibraryArtist {
  id: string;
  name: string;
  mbid?: string | null;
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
    setSelectedArtistId(null);
  };
  const offset = (page - 1) * PAGE_SIZE;
  const { data, isLoading, isError, isPlaceholderData } = useMatchingQueue(PAGE_SIZE, offset);
  const rerunMatching = useRerunMatching();
  const resolveIdentity = useResolveIdentity();
  const resolveArtist = useResolveArtist();

  // Selection is held as an id (not a frozen QueueArtist snapshot) so the
  // right-side panels re-render against the latest queue data after a resolve
  // mutation invalidates ["matching"] — without this, a curator who resolves
  // a title would keep seeing a stale NEEDS_REVIEW badge until they clicked
  // off the artist and back.
  const [selectedArtistId, setSelectedArtistId] = useState<string | null>(null);
  const [slideOverOpen, setSlideOverOpen] = useState(false);
  const [activeIdentityId, setActiveIdentityId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  // A single discriminated state replaces four separate open/targetId atoms
  // (mbSearchOpen + mbSearchTargetArtistId + librarySearchOpen +
  // librarySearchTargetArtistId).  Only one artist-search mode can be active
  // at a time; null means closed.
  //
  // targetArtistId is captured at open-time so a queue-selection change while
  // the slide-over is open cannot silently redirect the mutation to a different
  // broadcast artist than the one the curator initiated the search for.
  const [artistSearch, setArtistSearch] = useState<{
    mode: "mb-artist" | "artist";
    targetArtistId: string;
  } | null>(null);

  const artists: QueueArtist[] = data?.items ?? [];
  const total: number = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const selectedArtist: QueueArtist | null = selectedArtistId
    ? (artists.find((a) => a.id === selectedArtistId) ?? null)
    : null;

  // Auto-dismiss success banners after 3s; keep errors until the next user
  // action so the curator has time to read what failed. Re-runs whenever
  // `feedback` changes, so each new success starts its own timer.
  useEffect(() => {
    if (feedback?.kind !== "success") return;
    const t = setTimeout(() => setFeedback(null), 3000);
    return () => clearTimeout(t);
  }, [feedback]);

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
      setSelectedArtistId(null);
    }
  }, [page, totalPages, data, total, isPlaceholderData]);

  function handleFileSearch(identityId: string) {
    setActiveIdentityId(identityId);
    setSlideOverOpen(true);
  }

  function handleFileSelect(file: LibraryFile) {
    if (!activeIdentityId) return;
    resolveIdentity.mutate(
      {
        id: activeIdentityId,
        resolution: { match_status: "manual_matched", library_file_id: file.id },
      },
      {
        onSuccess: () => {
          const label = file.track_title?.trim() || file.file_path?.trim();
          setFeedback({
            kind: "success",
            message: label ? `Linked "${label}".` : "Linked file.",
          });
        },
        onError: (err) =>
          setFeedback({
            kind: "error",
            message: `Failed to link file: ${errorMessage(err)}`,
          }),
      },
    );
    setActiveIdentityId(null);
  }

  function handleOpenArtistSearch(mode: "mb-artist" | "artist") {
    if (!selectedArtist) return;
    setArtistSearch({ mode, targetArtistId: selectedArtist.id });
  }

  function handleCloseArtistSearch() {
    setArtistSearch(null);
  }

  // Shared resolve path for both MB and library artist search.
  // target_artist_id accepts either a MusicBrainz MBID or a local catalog UUID
  // (the backend stores both forms in matches.target_id — see resolve_artist
  // endpoint and artist_matching_service.py for the dual-form contract).
  function handleResolveFromArtistSearch(targetId: string, label?: string) {
    if (!artistSearch) return;
    resolveArtist.mutate(
      {
        id: artistSearch.targetArtistId,
        resolution: { match_status: "manual_matched", target_artist_id: targetId },
      },
      {
        onSuccess: () => {
          const trimmed = label?.trim();
          setFeedback({
            kind: "success",
            message: trimmed ? `Linked artist "${trimmed}".` : "Linked artist.",
          });
        },
        onError: (err) =>
          setFeedback({
            kind: "error",
            message: `Failed to link artist: ${errorMessage(err)}`,
          }),
      },
    );
    handleCloseArtistSearch();
  }

  function handleMbArtistSelect(mb: MbArtistResult) {
    handleResolveFromArtistSearch(mb.id, mb.name);
  }

  function handleLibraryArtistSelect(artist: LibraryArtist) {
    // Prefer mbid; fall back to local catalog UUID for local-only artists.
    // Trim first so a whitespace-padded MBID is never persisted as-is, and
    // an all-whitespace value is treated the same as null/empty.
    const targetId = artist.mbid?.trim() || artist.id;
    handleResolveFromArtistSearch(targetId, artist.name);
  }

  function handleRerun() {
    // perform_reset=true rewinds NEEDS_REVIEW + AUTO_REJECTED rows back to
    // PENDING and drops their stale matches before re-enqueueing — see
    // backend RunMatchingRequest. Manual decisions and AUTO_MATCHED rows are
    // preserved by the backend cohort filter.
    rerunMatching.mutate(
      { perform_reset: true },
      {
        onSuccess: (resp) => {
          // Re-run can reshuffle the queue — clear the selection so the right
          // panels don't act on a stale artist that may no longer exist.
          setPage(1);
          setSelectedArtistId(null);
          setFeedback({ kind: "success", message: rerunSuccessMessage(resp) });
        },
        onError: (err) => {
          setFeedback({ kind: "error", message: rerunErrorMessage(err) });
        },
      },
    );
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

      {feedback && (
        <div
          role="status"
          aria-live="polite"
          className={cn(
            "mx-4 mb-2 rounded-md border px-3 py-2 text-sm",
            feedback.kind === "success"
              ? "border-green-200 bg-green-50 text-green-700"
              : "border-red-200 bg-red-50 text-red-700",
          )}
        >
          {feedback.message}
        </div>
      )}

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
                    onClick={() => setSelectedArtistId(artist.id)}
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
                <ArtistPanel
                  artist={selectedArtist}
                  onSearchMusicBrainz={() => handleOpenArtistSearch("mb-artist")}
                  onSearchLibrary={() => handleOpenArtistSearch("artist")}
                />
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

      {/* Both artist-search modes share one lifecycle (handleCloseArtistSearch)
          so there is a single, unambiguous close path regardless of which
          mode is open. Only one can be open at a time via artistSearch.mode. */}
      <SearchSlideOver
        open={artistSearch?.mode === "mb-artist"}
        onClose={handleCloseArtistSearch}
        mode="mb-artist"
        onSelectMbArtist={handleMbArtistSelect}
      />
      <SearchSlideOver
        open={artistSearch?.mode === "artist"}
        onClose={handleCloseArtistSearch}
        mode="artist"
        onSelectArtist={handleLibraryArtistSelect}
      />
    </div>
  );
}
