import { MatchStatusBadge } from '@/components/ui/Badge'
import { useResolveArtist } from '@/api/matcher'
import type { QueueArtist } from '@/lib/schemas/matcher'
import type { MatchCandidate } from '@/lib/schemas/matches'

interface ArtistPanelProps {
  artist: QueueArtist
}

export function ArtistPanel({ artist }: ArtistPanelProps) {
  const resolveArtist = useResolveArtist()

  function handleAccept(candidate: MatchCandidate) {
    resolveArtist.mutate({
      id: artist.id,
      resolution: {
        match_status: 'MANUAL_MATCHED',
        target_artist_id: candidate.mbid,
      },
    })
  }

  function handleReject() {
    resolveArtist.mutate({
      id: artist.id,
      resolution: { match_status: 'MANUAL_REJECTED', target_artist_id: null },
    })
  }

  // Parse candidates — each item in the array may be null or a plain record.
  // We cast to MatchCandidate only when mbid + name exist.
  const candidates: MatchCandidate[] = (artist.candidates ?? []).flatMap(
    (raw) => {
      if (!raw || typeof raw !== 'object') return []
      const r = raw as Record<string, unknown>
      if (typeof r['mbid'] !== 'string' || typeof r['name'] !== 'string') return []
      return [
        {
          mbid: r['mbid'],
          name: r['name'],
          score: typeof r['score'] === 'number' ? r['score'] : 0,
          disambiguation:
            typeof r['disambiguation'] === 'string' ? r['disambiguation'] : undefined,
        },
      ]
    },
  )

  const isPending = resolveArtist.isPending

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5">
      {/* Artist identity info */}
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-lg font-semibold text-gray-900">
            {artist.original_name}
          </p>
          {artist.normalized_name !== artist.original_name && (
            <p className="truncate text-sm text-gray-500">
              {artist.normalized_name}
            </p>
          )}
        </div>
        <MatchStatusBadge status={artist.match_status} className="shrink-0" />
      </div>

      {/* Candidates */}
      {candidates.length > 0 ? (
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
                <p className="truncate text-sm font-medium text-gray-800">
                  {candidate.name}
                </p>
                {candidate.disambiguation && (
                  <p className="truncate text-xs text-gray-400">
                    {candidate.disambiguation}
                  </p>
                )}
                <p className="text-xs text-gray-400">
                  Score: {candidate.score.toFixed(2)}
                </p>
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
        <p className="mb-4 text-sm text-gray-400">No candidates available.</p>
      )}

      {/* Reject */}
      <button
        onClick={handleReject}
        disabled={isPending}
        className="w-full rounded-md border border-red-200 px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
      >
        Reject Artist
      </button>
    </div>
  )
}
