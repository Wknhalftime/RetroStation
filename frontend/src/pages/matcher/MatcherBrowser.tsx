import { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { PageHeader } from '@/components/ui/PageHeader'
import { EmptyState } from '@/components/ui/EmptyState'
import { Spinner } from '@/components/ui/Spinner'
import { MatchStatusBadge } from '@/components/ui/Badge'
import { cn } from '@/lib/utils'
import { useMatchingQueue, useRerunMatching, useResolveIdentity } from '@/api/matcher'
import { ArtistPanel } from '@/components/domain/matcher/ArtistPanel'
import { TitlePanel } from '@/components/domain/matcher/TitlePanel'
import { SearchSlideOver } from '@/components/domain/matcher/SearchSlideOver'
import type { QueueArtist } from '@/lib/schemas/matcher'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LibraryFile {
  id: string
  path: string
  title?: string | null
}

// ---------------------------------------------------------------------------
// MatcherBrowser
// ---------------------------------------------------------------------------

export function MatcherBrowser() {
  const { data, isLoading, isError } = useMatchingQueue(50, 0)
  const rerunMatching = useRerunMatching()
  const resolveIdentity = useResolveIdentity()

  const [selectedArtist, setSelectedArtist] = useState<QueueArtist | null>(null)
  const [slideOverOpen, setSlideOverOpen] = useState(false)
  const [activeIdentityId, setActiveIdentityId] = useState<string | null>(null)

  const artists: QueueArtist[] = data?.items ?? []

  function handleFileSearch(identityId: string) {
    setActiveIdentityId(identityId)
    setSlideOverOpen(true)
  }

  function handleFileSelect(file: LibraryFile) {
    if (!activeIdentityId) return
    resolveIdentity.mutate({
      id: activeIdentityId,
      resolution: { match_status: 'MANUAL_MATCHED', library_file_id: file.id },
    })
    setActiveIdentityId(null)
  }

  function handleRerun() {
    rerunMatching.mutate()
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Matcher"
        description="Resolve artist and title matches from the queue."
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

      {!isLoading && !isError && artists.length === 0 && (
        <div className="flex flex-1 items-center justify-center">
          <EmptyState
            title="Queue is empty"
            description="All artists have been resolved or the queue has not been populated yet."
          />
        </div>
      )}

      {!isLoading && !isError && artists.length > 0 && (
        <div className="flex min-h-0 flex-1 gap-4">
          {/* Left — artist list */}
          <aside className="w-80 shrink-0 overflow-y-auto rounded-lg border border-gray-200 bg-white">
            <div className="border-b border-gray-100 px-4 py-2">
              <p className="text-xs font-medium uppercase tracking-wide text-gray-400">
                Artists ({artists.length})
              </p>
            </div>
            <ul>
              {artists.map((artist) => (
                <li key={artist.id}>
                  <button
                    onClick={() => setSelectedArtist(artist)}
                    className={cn(
                      'flex w-full items-center justify-between gap-2 px-4 py-3 text-left hover:bg-gray-50',
                      selectedArtist?.id === artist.id && 'bg-blue-50',
                    )}
                  >
                    <span className="min-w-0 truncate text-sm font-medium text-gray-800">
                      {artist.original_name}
                    </span>
                    <MatchStatusBadge
                      status={artist.match_status}
                      className="shrink-0"
                    />
                  </button>
                </li>
              ))}
            </ul>
          </aside>

          {/* Right — panels */}
          <div className="flex min-w-0 flex-1 flex-col gap-4">
            {selectedArtist ? (
              <>
                <ArtistPanel artist={selectedArtist} />
                <TitlePanel
                  artist={selectedArtist}
                  onFileSearch={handleFileSearch}
                />
              </>
            ) : (
              <div className="flex flex-1 items-center justify-center rounded-lg border border-dashed border-gray-300 p-12 text-center">
                <p className="text-sm text-gray-400">
                  Select an artist from the list to begin.
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      <SearchSlideOver
        open={slideOverOpen}
        onClose={() => setSlideOverOpen(false)}
        mode="file"
        restrictArtistMbid={
          selectedArtist?.candidates?.[0] != null
            ? (selectedArtist.candidates[0] as Record<string, unknown>)['mbid'] as string | null
            : null
        }
        onSelectFile={handleFileSelect}
      />
    </div>
  )
}
