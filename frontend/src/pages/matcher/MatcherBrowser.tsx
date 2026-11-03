import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Search } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { EmptyState } from "@/components/ui/EmptyState";
import { Spinner } from "@/components/ui/Spinner";
import { MatchStatusBadge } from "@/components/ui/Badge";
import { cn } from "@/lib/utils";
import {
  listNotAnArtist,
  useMatchingQueue,
  useMatchingRunning,
  useRerunMatching,
  useResolveArtist,
  useResolveIdentity,
  type QueueSort,
} from "@/api/matcher";
import { ArtistPanel } from "@/components/domain/matcher/ArtistPanel";
import { TitlePanel } from "@/components/domain/matcher/TitlePanel";
import { SearchSlideOver } from "@/components/domain/matcher/SearchSlideOver";
import type { MbArtistResult, QueueArtist } from "@/lib/schemas/matcher";

// "undo": a confirmed Not an artist the page holds for UNDO_MS before sending it (phase 1c S9).
type Feedback = { kind: "success" | "error" | "undo"; message: string } | null;

// Not an artist's Undo (phase 1c S9; Lance, 2026-11-03): the listing is sent only once these
// are up, so an Undo inside them sends and writes nothing.
const UNDO_MS = 5000;

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// The button only queues a full re-check (spec 2026-10-05 §4.2, D1). The worker rewinds and
// re-matches in the background, and the queue fills in as its runs finish.
const RERUN_QUEUED_MESSAGE =
  "Re-check queued: undecided artists and titles are matched again in the background.";

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
// Debounce hook
// Mirrors the pattern in pages/library/ArtistBrowser.tsx — keep the two in
// sync if behaviour ever changes.
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
// MatcherBrowser
// ---------------------------------------------------------------------------

const PAGE_SIZE = 25;

function unlikelyToggleLabel(count: number): string {
  const artists = count === 1 ? "artist" : "artists";
  return `Show ${count.toLocaleString("en-US")} ${artists} with no likely match`;
}

export function MatcherBrowser() {
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const debouncedSearch = useDebounce(searchInput, 300);
  const [sort, setSort] = useState<QueueSort>("created_at");
  // Artists whose review items all lack a guess at 56% or above stay out of the list
  // unless the curator asks for them. Exceptions that stay in: an artist the curator
  // unmatched, and a song released by deleting its library file. A song the curator
  // rejected is parked as manual_rejected and is out of review until the next re-check
  // (so its artist leaves the main list). A matched artist whose songs are only
  // auto_rejected / manual_rejected (plus matched ones) has nothing to review and is
  // hidden here too, but is counted in the toggle (R7). Songs under 56% never reach
  // review: the matcher auto-rejects them (D6).
  const [includeUnlikely, setIncludeUnlikely] = useState(false);
  // The placeholder view (phase 1c S9): the names on the not-an-artist list instead of the
  // queue, so one can be taken off the list later.
  const [showPlaceholders, setShowPlaceholders] = useState(false);

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

  // Whenever the active search term or sort mode changes, jump back to page 1
  // and drop the selection — the previously selected artist may not appear in
  // the new filtered/sorted view, and right-side panels would otherwise act
  // on a row not shown in the sidebar.
  useEffect(() => {
    setPage(1);
    setSelectedArtistId(null);
  }, [debouncedSearch, sort, includeUnlikely, showPlaceholders]);

  const offset = (page - 1) * PAGE_SIZE;
  const { data, isLoading, isError, isPlaceholderData } = useMatchingQueue(
    PAGE_SIZE,
    offset,
    debouncedSearch,
    sort,
    includeUnlikely,
    showPlaceholders,
  );
  const rerunMatching = useRerunMatching();
  const matchingRunning = useMatchingRunning();
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

  // A confirmed Not an artist the page holds for its Undo (phase 1c S9): the name is hidden
  // from the list at once, and across refetches, until the listing has been sent and the
  // queue fetched again, or it is undone. A ref too, so leaving the page can send it.
  const queryClient = useQueryClient();
  const [held, setHeld] = useState<QueueArtist | null>(null);
  const heldRef = useRef<{ artist: QueueArtist; timer: ReturnType<typeof setTimeout> } | null>(
    null,
  );

  const artists: QueueArtist[] = (data?.items ?? []).filter((a) => a.id !== held?.id);
  const total: number = data?.total ?? 0;
  const unlikelyTotal: number = data?.unlikely_total ?? 0;
  const hasHiddenOrShownUnlikely = unlikelyTotal > 0 || includeUnlikely;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const selectedArtist: QueueArtist | null = selectedArtistId
    ? (artists.find((a) => a.id === selectedArtistId) ?? null)
    : null;

  // Auto-dismiss success banners after 3s; keep errors until the next user
  // action so the curator has time to read what failed. Re-runs whenever
  // `feedback` changes, so each new success starts its own timer. The Undo banner lasts as
  // long as the listing is held (UNDO_MS).
  useEffect(() => {
    if (feedback?.kind !== "success") return;
    const t = setTimeout(() => setFeedback(null), 3000);
    return () => clearTimeout(t);
  }, [feedback]);

  // Send a held listing: the banner goes, and the name stays hidden until the queue has been
  // fetched again without it; a failure shows the error and puts the name back.
  const sendListing = useCallback(
    (artist: QueueArtist) => {
      setFeedback(null);
      listNotAnArtist(artist.id)
        .then(() => queryClient.invalidateQueries({ queryKey: ["matching"] }))
        .then(() => setHeld((current) => (current?.id === artist.id ? null : current)))
        .catch((err: unknown) => {
          setHeld((current) => (current?.id === artist.id ? null : current));
          setFeedback({
            kind: "error",
            message: `Could not list ${artist.original_name} as not an artist: ${errorMessage(err)}`,
          });
        });
    },
    [queryClient],
  );

  function handleNotAnArtist(artist: QueueArtist) {
    // A listing already held is sent now: only one Undo is offered at a time.
    const before = heldRef.current;
    if (before !== null) {
      clearTimeout(before.timer);
      sendListing(before.artist);
    }
    const timer = setTimeout(() => {
      heldRef.current = null;
      sendListing(artist);
    }, UNDO_MS);
    heldRef.current = { artist, timer };
    setHeld(artist);
    setSelectedArtistId(null);
    setFeedback({ kind: "undo", message: `${artist.original_name} is listed as not an artist.` });
  }

  function handleUndo() {
    const current = heldRef.current;
    if (current === null) return;
    clearTimeout(current.timer);
    heldRef.current = null;
    setHeld(null);
    setFeedback(null);
  }

  // Leaving the page inside the Undo sends the listing then: the curator confirmed it.
  useEffect(
    () => () => {
      const current = heldRef.current;
      if (current === null) return;
      clearTimeout(current.timer);
      heldRef.current = null;
      void listNotAnArtist(current.artist.id).catch(() => undefined);
    },
    [],
  );

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
    // Queues rematch_undecided_task("all"): every undecided artist and title goes back to
    // pending and is matched again. A song the curator rejected (manual_rejected) is
    // undecided here and is re-scored without its rejected files. Matches and manually
    // rejected artists are never touched.
    rerunMatching.mutate(undefined, {
      onSuccess: () => {
        // The queue will reshuffle as the re-check runs: clear the selection so the right
        // panels don't act on a stale artist.
        setPage(1);
        setSelectedArtistId(null);
        setFeedback({ kind: "success", message: RERUN_QUEUED_MESSAGE });
      },
      onError: (err) => {
        // 503 carries the server's own "try again shortly" detail.
        setFeedback({ kind: "error", message: errorMessage(err) });
      },
    });
  }

  // Always reachable (an empty queue included), so a listed name can be taken off the list
  // after its Undo is gone (phase 1c S9, E4).
  const placeholderToggle = (
    <label className="flex items-center gap-2 text-xs text-gray-500">
      <input
        type="checkbox"
        checked={showPlaceholders}
        onChange={(e) => setShowPlaceholders(e.target.checked)}
        className="h-3.5 w-3.5 rounded border-gray-300"
      />
      Show placeholders
    </label>
  );

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Resolution Center"
        description="Link broadcast log names to MusicBrainz artists and local library files."
        actions={
          <button
            onClick={handleRerun}
            disabled={rerunMatching.isPending || matchingRunning}
            title={matchingRunning ? "A matching re-check is running" : undefined}
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
            "mx-4 mb-2 flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm",
            feedback.kind === "error"
              ? "border-red-200 bg-red-50 text-red-700"
              : "border-green-200 bg-green-50 text-green-700",
          )}
        >
          <span>{feedback.message}</span>
          {feedback.kind === "undo" && (
            <button
              type="button"
              onClick={handleUndo}
              className="shrink-0 rounded border border-green-300 px-2 py-0.5 text-xs font-medium hover:bg-green-100"
            >
              Undo
            </button>
          )}
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

      {/* When the queue has truly never had data (no search active and total
          is zero), show the centered empty state. If a search is active we
          fall through to the sidebar so the curator can edit/clear the term
          even when the filtered result is empty. */}
      {!isLoading &&
        !isError &&
        total === 0 &&
        debouncedSearch.length === 0 &&
        !hasHiddenOrShownUnlikely &&
        !showPlaceholders && (
        <div className="flex flex-1 flex-col items-center justify-center gap-3">
          <EmptyState
            title="Queue is empty"
            description="All artists have been resolved or the queue has not been populated yet."
          />
          {placeholderToggle}
        </div>
      )}

      {!isLoading &&
        !isError &&
        (total > 0 ||
          debouncedSearch.length > 0 ||
          hasHiddenOrShownUnlikely ||
          showPlaceholders) && (
        <div className="flex min-h-0 flex-1 gap-4">
          {/* Left — artist list */}
          <aside className="flex w-80 shrink-0 flex-col overflow-hidden rounded-lg border border-gray-200 bg-white">
            <div className="space-y-2 border-b border-gray-100 px-4 py-2">
              <div className="flex items-center justify-between">
                <p className="text-xs font-medium uppercase tracking-wide text-gray-400">
                  Artists ({artists.length})
                </p>
                {/* Sort toggle. Two-button segmented control — small enough that
                    a select would feel heavyweight; both options always visible. */}
                <div className="flex gap-0.5 rounded-md bg-gray-100 p-0.5 text-xs">
                  <button
                    type="button"
                    onClick={() => setSort("created_at")}
                    className={cn(
                      "rounded px-2 py-0.5",
                      sort === "created_at"
                        ? "bg-white font-medium text-gray-800 shadow-sm"
                        : "text-gray-500 hover:text-gray-700",
                    )}
                  >
                    Recent
                  </button>
                  <button
                    type="button"
                    onClick={() => setSort("name")}
                    className={cn(
                      "rounded px-2 py-0.5",
                      sort === "name"
                        ? "bg-white font-medium text-gray-800 shadow-sm"
                        : "text-gray-500 hover:text-gray-700",
                    )}
                  >
                    Name
                  </button>
                </div>
              </div>
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
                <input
                  type="text"
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  placeholder="Search artists…"
                  className="w-full rounded-md border border-gray-200 bg-white py-1.5 pl-7 pr-2 text-sm text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
              </div>
              {hasHiddenOrShownUnlikely && !showPlaceholders && (
                <label className="flex items-center gap-2 text-xs text-gray-500">
                  <input
                    type="checkbox"
                    checked={includeUnlikely}
                    onChange={(e) => setIncludeUnlikely(e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-gray-300"
                  />
                  {unlikelyToggleLabel(unlikelyTotal)}
                </label>
              )}
              {placeholderToggle}
            </div>
            <ul className="flex-1 overflow-y-auto">
              {artists.length === 0 && (
                <li className="px-4 py-6 text-center text-xs text-gray-400">
                  {debouncedSearch.length > 0
                    ? "No artists match this search."
                    : showPlaceholders
                      ? "No names are on the not-an-artist list."
                      : "No likely matches left to review."}
                </li>
              )}
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
                {/* Keyed by artist: a confirmation open for one artist never carries over
                    to the next one selected (phase 1b, S6). */}
                <ArtistPanel
                  key={selectedArtist.id}
                  artist={selectedArtist}
                  onSearchMusicBrainz={() => handleOpenArtistSearch("mb-artist")}
                  onSearchLibrary={() => handleOpenArtistSearch("artist")}
                  onCascadeDone={(message) => setFeedback({ kind: "success", message })}
                  listed={showPlaceholders}
                  onNotAnArtist={handleNotAnArtist}
                  onBackToQueue={(message) => setFeedback({ kind: "success", message })}
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
        // Never restricted to a suggestion the curator has not confirmed (phase 1c S8).
        restrictArtistMbid={null}
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
