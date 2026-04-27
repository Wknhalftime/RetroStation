import { MatchStatusBadge } from "@/components/ui/Badge";
import { useResolveArtist } from "@/api/matcher";
import type { QueueArtist } from "@/lib/schemas/matcher";
import type { MatchCandidate } from "@/lib/schemas/matches";

interface ArtistPanelProps {
  artist: QueueArtist;
  onSearchMusicBrainz?: () => void;
  onSearchLibrary?: () => void;
}

export function ArtistPanel({
  artist,
  onSearchMusicBrainz,
  onSearchLibrary,
}: ArtistPanelProps) {
  const resolveArtist = useResolveArtist();

  // The queue's broadened CTE now surfaces AUTO_MATCHED / MANUAL_MATCHED
  // parents that have at least one review-relevant child. For those rows the
  // artist-level decision is already settled — the curator's job is at the
  // child level — so we hide the artist-level Accept/Reject controls but keep
  // the search flows so a wrong auto-match can still be re-linked.
  //
  // isResolved deliberately excludes auto_rejected / manual_rejected: the
  // existing MANUAL_REJECTED cascade in resolve_artist already flips all
  // non-protected children to AUTO_REJECTED, so a rejected parent surfacing
  // through the broadened queue CTE with pending/needs_review children is
  // unreachable in steady state. If that invariant ever changes, extend
  // isResolved.
  const isResolved =
    artist.match_status === "auto_matched" ||
    artist.match_status === "manual_matched";

  function handleAccept(candidate: MatchCandidate) {
    resolveArtist.mutate({
      id: artist.id,
      resolution: {
        match_status: "manual_matched",
        target_artist_id: candidate.mbid,
      },
    });
  }

  function handleReject() {
    resolveArtist.mutate({
      id: artist.id,
      resolution: { match_status: "manual_rejected", target_artist_id: null },
    });
  }

  // Parse candidates — each item in the array may be null or a plain record.
  // We cast to MatchCandidate only when mbid + name exist.
  const candidates: MatchCandidate[] = (artist.candidates ?? []).flatMap((raw) => {
    if (!raw || typeof raw !== "object") return [];
    const r = raw as Record<string, unknown>;
    if (typeof r["mbid"] !== "string" || typeof r["name"] !== "string") return [];
    return [
      {
        mbid: r["mbid"],
        name: r["name"],
        score: typeof r["score"] === "number" ? r["score"] : 0,
        disambiguation: typeof r["disambiguation"] === "string" ? r["disambiguation"] : undefined,
      },
    ];
  });

  const isPending = resolveArtist.isPending;

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

      {/* Prefer the human-formatted detail; fall back to the stable code.
          Use a truthiness check on each so an empty-string reason_detail
          doesn't render as a blank amber line when reason_code is present. */}
      {(() => {
        const reasonText = artist.reason_detail || artist.reason_code;
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
                key={candidate.mbid}
                className="flex items-center justify-between gap-3 rounded-md border border-gray-100 bg-gray-50 px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-gray-800">{candidate.name}</p>
                  {candidate.disambiguation && (
                    <p className="truncate text-xs text-gray-400">{candidate.disambiguation}</p>
                  )}
                  <p className="text-xs text-gray-400">Score: {candidate.score.toFixed(2)}</p>
                </div>
                <button
                  onClick={() => handleAccept(candidate)}
                  disabled={isPending}
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

      {/* Reject — hidden for resolved parents: rejecting a resolved artist
          would cascade children to auto_rejected via _PROTECTED_STATUSES,
          which is destructive when the curator's actual goal is per-child
          review. To override a wrong auto-match, use the Search… buttons
          above instead. */}
      {!isResolved && (
        <button
          onClick={handleReject}
          disabled={isPending}
          className="w-full rounded-md border border-red-200 px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
        >
          Reject Artist
        </button>
      )}
    </div>
  );
}
