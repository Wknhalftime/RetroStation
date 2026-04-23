import { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { PageHeader } from '@/components/ui/PageHeader'
import { EmptyState } from '@/components/ui/EmptyState'
import { Spinner } from '@/components/ui/Spinner'
import { MatchStatusBadge } from '@/components/ui/Badge'
import { cn } from '@/lib/utils'
import {
  useMatchingQueue,
  useRerunMatching,
  useResolveArtist,
  useResolveIdentity,
} from '@/api/matcher'
import { ArtistPanel } from '@/components/domain/matcher/ArtistPanel'
import { TitlePanel } from '@/components/domain/matcher/TitlePanel'
import { SearchSlideOver } from '@/components/domain/matcher/SearchSlideOver'
import type { MbArtistResult, QueueArtist } from '@/lib/schemas/matcher'

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

const PAGE_SIZE = 25

export function MatcherBrowser() {
  const [page, setPage] = useState(1)
  const offset = (page - 1) * PAGE_SIZE
  const { data, isLoading, isError } = useMatchingQueue(PAGE_SIZE, offset)
  const rerunMatching = useRerunMatching()
  const resolveIdentity = useResolveIdentity()
  const resolveArtist = useResolveArtist()

  const [selectedArtist, setSelectedArtist] = useState<QueueArtist | null>(null)
  const [slideOverOpen, setSlideOverOpen] = useState(false)
  const [activeIdentityId, setActiveIdentityId] = useState<string | null>(null)
  const [mbSearchOpen, setMbSearchOpen] = useState(false)

  const artists: QueueArtist[] = data?.items ?? []
  const total: number = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  function handleFileSearch(identityId: string) {
    setActiveIdentityId(identityId)
    setSlideOverOpen(true)
  }

  function handleFileSelect(file: LibraryFile) {
    if (!activeIdentityId) return
    resolveIdentity.mutate({
      id: activeIdentityId,
      resolution: { match_status: 'manual_matched', library_file_id: file.id },
    })
    setActiveIdentityId(null)
  }

  function handleMbArtistSelect(mb: MbArtistResult) {
    if (!selectedArtist) return
    resolveArtist.mutate({
      id: selectedArtist.id,
      resolution: { match_status: 'manual_matched', target_artist_id: mb.id },
    })
    setMbSearchOpen(false)
  }

  function handleRerun() {
    rerunMatching.mutate(undefined, {
      onSuccess: () => setPage(1),
    })
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
            <div className="flex items-center justify-between border-t border-gray-100 px-4 py-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="text-xs text-gray-500 disabled:opacity-40"
              >
                ← Prev
              </button>
              <span className="text-xs text-gray-400">
                Page {page} of {totalPages} ({total} total)
              </span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
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
                  onSearchMusicBrainz={() => setMbSearchOpen(true)}
                />
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

      <SearchSlideOver
        open={mbSearchOpen}
        onClose={() => setMbSearchOpen(false)}
        mode="mb-artist"
        onSelectMbArtist={handleMbArtistSelect}
      />
    </div>
  )
}
