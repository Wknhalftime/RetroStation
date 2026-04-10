import { MatchStatusBadge } from '@/components/ui/Badge'
import { useResolveIdentity } from '@/api/matcher'
import type { QueueArtist, QueueIdentity } from '@/lib/schemas/matcher'

interface TitlePanelProps {
  artist: QueueArtist | null
  onFileSearch: (identityId: string) => void
}

export function TitlePanel({ artist, onFileSearch }: TitlePanelProps) {
  const resolveIdentity = useResolveIdentity()

  const artistResolved =
    artist !== null &&
    (artist.match_status === 'MANUAL_MATCHED' || artist.match_status === 'MATCHED')

  if (!artist) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5">
        <p className="text-sm text-gray-400">Select an artist to view titles.</p>
      </div>
    )
  }

  if (!artistResolved) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5">
        <p className="text-sm text-gray-500">
          Resolve the artist first before managing titles.
        </p>
      </div>
    )
  }

  const identities: QueueIdentity[] = artist.identities ?? []

  function handleReject(identity: QueueIdentity) {
    resolveIdentity.mutate({
      id: identity.id,
      resolution: { match_status: 'MANUAL_REJECTED', library_file_id: null },
    })
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5">
      <p className="mb-3 text-xs font-medium uppercase tracking-wide text-gray-400">
        Titles ({identities.length})
      </p>

      {identities.length === 0 ? (
        <p className="text-sm text-gray-400">No identities for this artist.</p>
      ) : (
        <div className="space-y-2">
          {identities.map((identity) => (
            <div
              key={identity.id}
              className="rounded-md border border-gray-100 bg-gray-50 px-3 py-2"
            >
              <div className="mb-2 flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-gray-800">
                    {identity.original_title}
                  </p>
                  {identity.match_tier && (
                    <p className="text-xs text-gray-400">
                      Tier: {identity.match_tier}
                    </p>
                  )}
                </div>
                <MatchStatusBadge
                  status={identity.match_status}
                  className="shrink-0"
                />
              </div>

              <div className="flex gap-2">
                <button
                  onClick={() => onFileSearch(identity.id)}
                  className="rounded bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-700"
                >
                  Find File
                </button>
                <button
                  onClick={() => handleReject(identity)}
                  disabled={resolveIdentity.isPending}
                  className="rounded border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
                >
                  Reject
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
