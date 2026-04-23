import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { apiFetch } from '@/api/client'
import { Spinner } from '@/components/ui/Spinner'
import { useMbArtistSearch } from '@/api/matcher'
import type { MbArtistResult } from '@/lib/schemas/matcher'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LibraryArtist {
  id: string
  name: string
  mbid?: string | null
}

interface LibraryFile {
  id: string
  path: string
  title?: string | null
}

type SearchMode = 'artist' | 'file' | 'mb-artist'

interface SearchSlideOverProps {
  open: boolean
  onClose: () => void
  mode: SearchMode
  restrictArtistMbid?: string | null
  onSelectArtist?: (artist: LibraryArtist) => void
  onSelectFile?: (file: LibraryFile) => void
  onSelectMbArtist?: (mb: MbArtistResult) => void
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SearchSlideOver({
  open,
  onClose,
  mode,
  restrictArtistMbid,
  onSelectArtist,
  onSelectFile,
  onSelectMbArtist,
}: SearchSlideOverProps) {
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [results, setResults] = useState<(LibraryArtist | LibraryFile)[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [restrictToArtist, setRestrictToArtist] = useState(true)
  const inputRef = useRef<HTMLInputElement>(null)

  // MusicBrainz artist search — active only when mode === 'mb-artist'.
  const mbSearch = useMbArtistSearch(mode === 'mb-artist' ? debouncedQuery : '')
  const mbResults: MbArtistResult[] = mbSearch.data ?? []

  // Focus input when opened
  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50)
      setQuery('')
      setDebouncedQuery('')
      setResults([])
      setError(null)
    }
  }, [open])

  // Debounce query — 300 ms
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 300)
    return () => clearTimeout(timer)
  }, [query])

  // Fetch results when debounced query changes
  useEffect(() => {
    // mb-artist mode is served by useMbArtistSearch; skip the library fetch.
    if (mode === 'mb-artist') {
      return
    }

    if (!debouncedQuery.trim()) {
      setResults([])
      return
    }

    const controller = new AbortController()
    setLoading(true)
    setError(null)

    const params = new URLSearchParams({
      search: debouncedQuery.trim(),
      limit: '20',
    })

    if (mode === 'file' && restrictToArtist && restrictArtistMbid) {
      params.set('artist_mbid', restrictArtistMbid)
    }

    apiFetch<{ items?: (LibraryArtist | LibraryFile)[]; data?: (LibraryArtist | LibraryFile)[] } | (LibraryArtist | LibraryFile)[]>(
      `/api/v1/library/artists?${params.toString()}`,
    )
      .then((res) => {
        if (!controller.signal.aborted) {
          if (Array.isArray(res)) {
            setResults(res)
          } else if (Array.isArray((res as { items?: unknown[] }).items)) {
            setResults((res as { items: (LibraryArtist | LibraryFile)[] }).items)
          } else if (Array.isArray((res as { data?: unknown[] }).data)) {
            setResults((res as { data: (LibraryArtist | LibraryFile)[] }).data)
          } else {
            setResults([])
          }
        }
      })
      .catch((err: Error) => {
        if (!controller.signal.aborted) {
          setError(err.message ?? 'Search failed')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })

    return () => controller.abort()
  }, [debouncedQuery, mode, restrictToArtist, restrictArtistMbid])

  // Close on Escape
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])

  function handleSelect(item: LibraryArtist | LibraryFile) {
    if (mode === 'artist') {
      onSelectArtist?.(item as LibraryArtist)
    } else {
      onSelectFile?.(item as LibraryFile)
    }
    onClose()
  }

  function renderResult(item: LibraryArtist | LibraryFile, idx: number) {
    const isArtist = mode === 'artist'
    const artist = item as LibraryArtist
    const file = item as LibraryFile

    return (
      <button
        key={idx}
        onClick={() => handleSelect(item)}
        className="w-full rounded-md px-3 py-2 text-left hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        {isArtist ? (
          <p className="truncate text-sm text-gray-800">{artist.name}</p>
        ) : (
          <>
            <p className="truncate text-sm font-medium text-gray-800">
              {file.title ?? 'Untitled'}
            </p>
            <p className="truncate text-xs text-gray-400">{file.path}</p>
          </>
        )}
      </button>
    )
  }

  return (
    <>
      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/20"
          aria-hidden="true"
          onClick={onClose}
        />
      )}

      {/* Slide-over panel */}
      <aside
        className={cn(
          'fixed right-0 top-0 z-50 flex h-full w-96 flex-col border-l border-gray-200 bg-white shadow-xl transition-transform duration-200',
          open ? 'translate-x-0' : 'translate-x-full',
        )}
        aria-label={
          mode === 'artist'
            ? 'Search artists'
            : mode === 'mb-artist'
              ? 'Search MusicBrainz artists'
              : 'Search files'
        }
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
          <h2 className="text-base font-semibold text-gray-900">
            {mode === 'artist'
              ? 'Find Artist'
              : mode === 'mb-artist'
                ? 'Find Artist on MusicBrainz'
                : 'Find File'}
          </h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-gray-400 hover:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Search input */}
        <div className="border-b border-gray-100 px-4 py-3">
          <input
            ref={inputRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={
              mode === 'artist'
                ? 'Search artists…'
                : mode === 'mb-artist'
                  ? 'Search MusicBrainz…'
                  : 'Search files…'
            }
            className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />

          {/* Restrict to confirmed artist checkbox — file mode only */}
          {mode === 'file' && restrictArtistMbid && (
            <label className="mt-2 flex items-center gap-2 text-xs text-gray-600">
              <input
                type="checkbox"
                checked={restrictToArtist}
                onChange={(e) => setRestrictToArtist(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              Restrict to confirmed artist
            </label>
          )}
        </div>

        {/* Results */}
        <div className="flex-1 overflow-y-auto px-3 py-2">
          {mode === 'mb-artist' ? (
            <>
              {mbSearch.isLoading && (
                <div className="flex items-center justify-center py-8">
                  <Spinner className="h-6 w-6 text-gray-400" />
                </div>
              )}
              {mbSearch.isError && !mbSearch.isLoading && (
                <p className="px-1 py-4 text-sm text-red-500">
                  {mbSearch.error instanceof Error
                    ? mbSearch.error.message
                    : 'Search failed'}
                </p>
              )}
              {/* Gate on trimmed query so "   " doesn't show a false-negative
                  empty state when no search actually ran. */}
              {!mbSearch.isLoading &&
                !mbSearch.isError &&
                mbResults.length === 0 &&
                debouncedQuery.trim() && (
                  <p className="px-1 py-4 text-sm text-gray-400">
                    No results found.
                  </p>
                )}
              {!mbSearch.isLoading && !mbSearch.isError && mbResults.length > 0 && (
                <div className="space-y-0.5">
                  {mbResults.map((item) => (
                    <button
                      key={item.id}
                      onClick={() => {
                        onSelectMbArtist?.(item)
                        onClose()
                      }}
                      className="w-full rounded-md px-3 py-2 text-left hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    >
                      <p className="truncate text-sm font-medium text-gray-800">
                        {item.name}
                      </p>
                      {item.disambiguation && (
                        <p className="truncate text-xs text-gray-400">
                          {item.disambiguation}
                        </p>
                      )}
                      <p className="text-xs text-gray-400">Score: {item.score}%</p>
                    </button>
                  ))}
                </div>
              )}
            </>
          ) : (
            <>
              {loading && (
                <div className="flex items-center justify-center py-8">
                  <Spinner className="h-6 w-6 text-gray-400" />
                </div>
              )}
              {error && !loading && (
                <p className="px-1 py-4 text-sm text-red-500">{error}</p>
              )}
              {!loading && !error && results.length === 0 && debouncedQuery && (
                <p className="px-1 py-4 text-sm text-gray-400">No results found.</p>
              )}
              {!loading && !error && results.length > 0 && (
                <div className="space-y-0.5">
                  {results.map((item, idx) => renderResult(item, idx))}
                </div>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  )
}
