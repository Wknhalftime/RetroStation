import { useEffect, useRef, useState } from "react";
import { MatchStatusBadge } from "@/components/ui/Badge";
import {
  type CascadeAction,
  useCascadePreview,
  useResolveArtist,
  useUnlistNotAnArtist,
  useUnmatchArtist,
} from "@/api/matcher";
import {
  CascadeCountsSchema,
  type CascadeCounts,
  type QueueArtist,
  type SuggestedLink,
} from "@/lib/schemas/matcher";

const FINALIZED_ARTIST_STATUSES = new Set([
  "auto_matched",
  "manual_matched",
  "auto_rejected",
  "manual_rejected",
]);

// Reason codes said in words when the row has no detail of its own.
const ARTIST_REASON_LABELS: Record<string, string> = {
  LINK_REJECTED: "The matcher found an artist you rejected for this name",
  LINK_SUGGESTED: "Suggested artists below: accept one, or search",
  LINK_REMOVED: "Its link was removed: pick an artist",
};

function songs(n: number): string {
  return n === 1 ? "1 song" : `${n} songs`;
}

function plays(n: number): string {
  return n === 1 ? "1 play" : `${n} plays`;
}

function stayCount(counts: CascadeCounts): number {
  return counts.kept.manual + counts.kept.same_name;
}

/** "N songs (P plays) go off air; M stay" (phase 1b, S6). */
function previewText(counts: CascadeCounts): string {
  const { lines, plays: played } = counts.off_air;
  return `${songs(lines)} (${plays(played)}) go off air; ${stayCount(counts)} stay`;
}

/** The action's own counts, which may differ from the preview's (E2). */
function doneText(verb: string, name: string, counts: CascadeCounts | null): string {
  if (counts === null) return `${verb} ${name}.`;
  const { lines, plays: played } = counts.off_air;
  return (
    `${verb} ${name}: ${songs(lines)} (${plays(played)}) went off air; ` +
    `${stayCount(counts)} stayed.`
  );
}

interface ArtistPanelProps {
  artist: QueueArtist;
  onSearchMusicBrainz?: () => void;
  onSearchLibrary?: () => void;
  /** Called with what a confirmed Reject or Unmatch did, in words (E2). */
  onCascadeDone?: (message: string) => void;
  /** The name is on the not-an-artist list: the placeholder view (phase 1c S9). */
  listed?: boolean;
  /** Called once a confirmed Not an artist is the page's to send, after its Undo (S9). */
  onNotAnArtist?: (artist: QueueArtist) => void;
  /** Called, in words, when the placeholder view's "Back to the queue" is done. */
  onBackToQueue?: (message: string) => void;
}

export function ArtistPanel({
  artist,
  onSearchMusicBrainz,
  onSearchLibrary,
  onCascadeDone,
  listed = false,
  onNotAnArtist,
  onBackToQueue,
}: ArtistPanelProps) {
  const resolveArtist = useResolveArtist();
  const unmatchArtist = useUnmatchArtist();
  const unlist = useUnlistNotAnArtist();
  // The whole-name action awaiting confirmation; null when no confirmation is open.
  const [confirming, setConfirming] = useState<CascadeAction | null>(null);
  const preview = useCascadePreview(artist.id, confirming);
  // Not an artist's own confirmation (phase 1c S9), and whether its Confirm was pressed, so
  // a second press before the dialog closes hands nothing over again (E4).
  const [confirmingNotAnArtist, setConfirmingNotAnArtist] = useState(false);
  const handedOver = useRef(false);
  // Focus Cancel when a confirmation opens, so Enter never confirms by accident.
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (confirming !== null || confirmingNotAnArtist) cancelRef.current?.focus();
  }, [confirming, confirmingNotAnArtist]);

  function openNotAnArtist() {
    handedOver.current = false;
    setConfirmingNotAnArtist(true);
  }

  function handleNotAnArtist() {
    if (handedOver.current) return;
    handedOver.current = true;
    setConfirmingNotAnArtist(false);
    onNotAnArtist?.(artist);
  }

  function handleBackToQueue() {
    unlist.mutate(
      { id: artist.id },
      { onSuccess: () => onBackToQueue?.(`${artist.original_name} is back in the queue.`) }
    );
  }

  // The placeholder view (S9): a listed name is never linked (Resolve answers 409), so the
  // only decision it offers is taking the name off the list.
  if (listed) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate text-lg font-semibold text-gray-900">{artist.original_name}</p>
            <p className="text-sm text-gray-500">
              Not an artist: hidden from the queue and the missing-matches report, and never
              matched.
            </p>
          </div>
          <MatchStatusBadge status={artist.match_status} className="shrink-0" />
        </div>
        {unlist.error && <p className="mb-2 text-sm text-red-600">{unlist.error.message}</p>}
        <button
          onClick={handleBackToQueue}
          disabled={unlist.isPending}
          className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
        >
          Back to the queue
        </button>
      </div>
    );
  }

  // The queue's broadened CTE now surfaces AUTO_MATCHED / MANUAL_MATCHED
  // parents that have at least one review-relevant child. For those rows the
  // artist-level decision is already settled — the curator's job is at the
  // child level — so we hide the artist-level Accept/Reject controls but keep
  // the search flows so a wrong auto-match can still be re-linked.
  //
  // isResolved deliberately excludes auto_rejected / manual_rejected: a rejected
  // parent rarely surfaces through the broadened queue CTE with pending or
  // needs_review children. If that ever changes, extend isResolved.
  const isResolved =
    artist.match_status === "auto_matched" ||
    artist.match_status === "manual_matched";
  // Unmatch is offered for any finalized state (matched OR rejected): it puts the artist
  // back in review, and with it every song that is not manual or matched to a same-name file.
  const isFinalized = FINALIZED_ARTIST_STATUSES.has(artist.match_status);

  // Accept is the manual resolve, to the suggested link's artist by its catalog id (phase
  // 1c S8): a suggestion may be a local artist with no MBID.
  function handleAccept(candidate: SuggestedLink) {
    resolveArtist.mutate({
      id: artist.id,
      resolution: {
        match_status: "manual_matched",
        target_artist_id: candidate.artist_id,
      },
    });
  }

  function closeConfirmation() {
    resolveArtist.reset();
    unmatchArtist.reset();
    setConfirming(null);
  }

  function handleConfirm() {
    if (confirming === null || resolveArtist.isPending || unmatchArtist.isPending) return;
    const verb = confirming === "reject" ? "Rejected" : "Unmatched";
    const onSuccess = (result: { cascade?: unknown }) => {
      const parsed = CascadeCountsSchema.safeParse(result.cascade);
      onCascadeDone?.(doneText(verb, artist.original_name, parsed.success ? parsed.data : null));
      setConfirming(null);
    };
    if (confirming === "reject") {
      resolveArtist.mutate(
        {
          id: artist.id,
          resolution: { match_status: "manual_rejected", target_artist_id: null },
        },
        { onSuccess }
      );
    } else {
      unmatchArtist.mutate({ id: artist.id }, { onSuccess });
    }
  }

  // The name's suggested links, best first (phase 1c S8).
  const candidates = artist.candidates;

  const isPending = resolveArtist.isPending || unmatchArtist.isPending;
  const actionError =
    confirming === "reject"
      ? resolveArtist.error
      : confirming === "unmatch"
        ? unmatchArtist.error
        : null;

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5">
      {/* Artist identity info */}
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-lg font-semibold text-gray-900">{artist.original_name}</p>
          {artist.normalized_name !== artist.original_name && (
            <p className="truncate text-sm text-gray-500">{artist.normalized_name}</p>
          )}
        </div>
        <MatchStatusBadge status={artist.match_status} className="shrink-0" />
      </div>

      {/* Prefer the human-formatted detail; then a code said in words; then the stable code.
          Use a truthiness check on each so an empty-string reason_detail
          doesn't render as a blank amber line when reason_code is present. */}
      {(() => {
        const code = artist.reason_code ?? "";
        const reasonText = artist.reason_detail || ARTIST_REASON_LABELS[code] || code;
        return reasonText ? (
          <p className="mt-1 mb-3 text-xs text-amber-600">⚠ {reasonText}</p>
        ) : null;
      })()}

      {/* Candidates — hidden for resolved parents: the auto-match has already
          chosen a target, replaying the candidate panel invites accidental
          no-op promotions to manual_matched. */}
      {!isResolved &&
        (candidates.length > 0 ? (
          <div className="mb-4 space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-gray-400">
              Candidates
            </p>
            {candidates.map((candidate) => (
              <div
                key={candidate.artist_id}
                className="flex items-center justify-between gap-3 rounded-md border border-gray-100 bg-gray-50 px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-gray-800">{candidate.name}</p>
                  {candidate.disambiguation && (
                    <p className="truncate text-xs text-gray-400">{candidate.disambiguation}</p>
                  )}
                  <p className="text-xs text-gray-400">
                    {candidate.score}% · {candidate.reason}
                  </p>
                </div>
                <button
                  onClick={() => handleAccept(candidate)}
                  disabled={isPending}
                  aria-label={`Accept ${candidate.name}`}
                  className="shrink-0 rounded bg-green-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
                >
                  Accept
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="mb-2 text-sm text-gray-400">No candidates found automatically.</p>
        ))}

      {/* Manual lookup actions — always visible so curators can override an
          auto-suggestion or rescue an empty-candidates artist. */}
      {(onSearchLibrary || onSearchMusicBrainz) && (
        <div className="mb-4 flex flex-wrap gap-2">
          {onSearchLibrary && (
            <button
              onClick={onSearchLibrary}
              className="rounded bg-gray-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-gray-800"
            >
              Search Library…
            </button>
          )}
          {onSearchMusicBrainz && (
            <button
              onClick={onSearchMusicBrainz}
              className="rounded bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-700"
            >
              Search MusicBrainz…
            </button>
          )}
        </div>
      )}

      {/* Reject and Unmatch ask first (phase 1b, S6): the confirmation shows how many songs
          would go off air, from the preview, and runs the action only on Confirm. Not an
          artist asks first too (phase 1c S9); its Confirm hands the name to the page, which
          sends it once its five-second Undo is over. */}
      {confirmingNotAnArtist ? (
        <div
          role="alertdialog"
          aria-labelledby="artist-not-an-artist-title"
          className="rounded-md border border-amber-200 bg-amber-50 p-3"
        >
          <p id="artist-not-an-artist-title" className="text-sm font-medium text-gray-900">
            {artist.original_name} is not an artist?
          </p>
          <p className="mt-1 text-sm text-gray-700">
            It leaves the queue and the missing-matches report, and its links are removed (a
            pair you rejected stays rejected). No run matches it or its songs. You can undo it
            for five seconds, and later through Show placeholders.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              onClick={handleNotAnArtist}
              className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700"
            >
              Confirm Not an artist
            </button>
            <button
              ref={cancelRef}
              onClick={() => setConfirmingNotAnArtist(false)}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : confirming !== null ? (
        <div
          role="alertdialog"
          aria-labelledby="artist-cascade-title"
          className="rounded-md border border-amber-200 bg-amber-50 p-3"
        >
          <p id="artist-cascade-title" className="text-sm font-medium text-gray-900">
            {confirming === "reject" ? "Reject" : "Unmatch"} {artist.original_name}?
          </p>
          <p className="mt-1 text-sm text-gray-700">
            {preview.isPending
              ? "Counting the songs that would go off air…"
              : preview.isError
                ? `Could not count the songs that would go off air: ${preview.error.message}`
                : `${previewText(preview.data)}. Songs matched by hand or to the artist's own files stay on air.`}
          </p>
          {actionError && <p className="mt-1 text-sm text-red-600">{actionError.message}</p>}
          <div className="mt-3 flex gap-2">
            {preview.isSuccess && (
              <button
                onClick={handleConfirm}
                disabled={isPending}
                className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
              >
                Confirm {confirming === "reject" ? "Reject" : "Unmatch"}
              </button>
            )}
            <button
              ref={cancelRef}
              onClick={closeConfirmation}
              disabled={isPending}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          {/* Reject — hidden for resolved parents, whose decision is at the song level; to
              override a wrong auto-match, use the Search… buttons above. */}
          {!isResolved && (
            <button
              onClick={() => setConfirming("reject")}
              disabled={isPending}
              className="w-full rounded-md border border-red-200 px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
            >
              Reject Artist
            </button>
          )}

          {/* Unmatch: hidden until the artist itself is in a finalized state — pending /
              needs_review have nothing to revert and the backend would 409. */}
          {isFinalized && (
            <button
              onClick={() => setConfirming("unmatch")}
              disabled={isPending}
              className="mt-2 w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
              aria-label={`Unmatch artist ${artist.original_name}`}
            >
              Unmatch artist
            </button>
          )}

          {/* Not an artist (phase 1c S9): for any status, a matched placeholder such as
              VARIOUS too. */}
          {onNotAnArtist && (
            <button
              onClick={openNotAnArtist}
              disabled={isPending}
              className="mt-2 w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
            >
              Not an artist
            </button>
          )}
        </>
      )}
    </div>
  );
}
