import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '@/api/client'
import type {
  MatchingQueue,
  ArtistResolution,
  IdentityResolution,
  ResolveResult,
  MbArtistResult,
} from '@/lib/schemas/matcher'

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

const matchingQueueKey = (limit: number, offset: number) =>
  ['matching', 'queue', { limit, offset }] as const

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useMatchingQueue(limit = 50, offset = 0) {
  return useQuery<MatchingQueue>({
    queryKey: matchingQueueKey(limit, offset),
    queryFn: () =>
      apiFetch<MatchingQueue>(
        `/api/v1/matching/queue?limit=${limit}&offset=${offset}`,
      ),
  })
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

interface ResolveArtistVariables {
  id: string
  resolution: ArtistResolution
}

export function useResolveArtist() {
  const queryClient = useQueryClient()
  return useMutation<ResolveResult, Error, ResolveArtistVariables>({
    mutationFn: ({ id, resolution }) =>
      apiFetch<ResolveResult>(`/api/v1/matching/artists/${id}/resolve`, {
        method: 'POST',
        body: JSON.stringify(resolution),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['matching'] })
    },
  })
}

interface ResolveIdentityVariables {
  id: string
  resolution: IdentityResolution
}

export function useResolveIdentity() {
  const queryClient = useQueryClient()
  return useMutation<ResolveResult, Error, ResolveIdentityVariables>({
    mutationFn: ({ id, resolution }) =>
      apiFetch<ResolveResult>(`/api/v1/matching/identities/${id}/resolve`, {
        method: 'POST',
        body: JSON.stringify(resolution),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['matching'] })
    },
  })
}

// ---------------------------------------------------------------------------
// MusicBrainz artist search
// ---------------------------------------------------------------------------

export function useMbArtistSearch(query: string) {
  return useQuery<MbArtistResult[]>({
    queryKey: ['mb-artist-search', query],
    queryFn: async () => {
      if (!query.trim()) return []
      const body = await apiFetch<{ items: MbArtistResult[] }>(
        `/api/v1/matching/mb-artists?query=${encodeURIComponent(query)}`,
      )
      return body.items
    },
    enabled: query.trim().length > 0,
    staleTime: 30_000,
  })
}

export function useRerunMatching() {
  const queryClient = useQueryClient()
  return useMutation<unknown, Error, void>({
    mutationFn: () =>
      apiFetch<unknown>('/api/v1/matching/run', { method: 'POST' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['matching'] })
    },
  })
}
