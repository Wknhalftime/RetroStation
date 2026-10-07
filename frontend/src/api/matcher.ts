import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { MbArtistSearchResponseSchema } from "@/lib/schemas/matcher";
import type {
  MatchingQueue,
  ArtistResolution,
  IdentityResolution,
  ResolveResult,
  MbArtistResult,
} from "@/lib/schemas/matcher";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

export type QueueSort = "created_at" | "name";

const matchingQueueKey = (
  limit: number,
  offset: number,
  search: string,
  sort: QueueSort,
  includeUnlikely: boolean,
) => ["matching", "queue", { limit, offset, search, sort, includeUnlikely }] as const;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useMatchingQueue(
  limit = 50,
  offset = 0,
  search: string = "",
  sort: QueueSort = "created_at",
  includeUnlikely = false,
) {
  // Trim once and feed both the cache key and the URL through the same value
  // so "  prince" and "prince" share a cache entry and a single request.
  const trimmedSearch = search.trim();
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    sort,
    include_unlikely: String(includeUnlikely),
  });
  if (trimmedSearch.length > 0) {
    params.set("search", trimmedSearch);
  }
  return useQuery<MatchingQueue>({
    queryKey: matchingQueueKey(limit, offset, trimmedSearch, sort, includeUnlikely),
    queryFn: () => apiFetch<MatchingQueue>(`/api/v1/matching/queue?${params.toString()}`),
    placeholderData: keepPreviousData,
  });
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

interface ResolveArtistVariables {
  id: string;
  resolution: ArtistResolution;
}

export function useResolveArtist() {
  const queryClient = useQueryClient();
  return useMutation<ResolveResult, Error, ResolveArtistVariables>({
    mutationFn: ({ id, resolution }) =>
      apiFetch<ResolveResult>(`/api/v1/matching/artists/${id}/resolve`, {
        method: "POST",
        body: JSON.stringify(resolution),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["matching"] });
    },
  });
}

interface ResolveIdentityVariables {
  id: string;
  resolution: IdentityResolution;
}

export function useResolveIdentity() {
  const queryClient = useQueryClient();
  return useMutation<ResolveResult, Error, ResolveIdentityVariables>({
    mutationFn: ({ id, resolution }) =>
      apiFetch<ResolveResult>(`/api/v1/matching/identities/${id}/resolve`, {
        method: "POST",
        body: JSON.stringify(resolution),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["matching"] });
    },
  });
}

// Unmatch reverts a finalized match (auto/manual matched or rejected) back to
// NEEDS_REVIEW with reason_code=USER_UNMATCHED. The matches row is hard-deleted
// server-side. Unmatching an artist cascades to ALL child identities regardless
// of status — see backend/routers/matching.py::unmatch_artist.

interface UnmatchVariables {
  id: string;
}

export function useUnmatchArtist() {
  const queryClient = useQueryClient();
  return useMutation<ResolveResult, Error, UnmatchVariables>({
    mutationFn: ({ id }) =>
      apiFetch<ResolveResult>(`/api/v1/matching/artists/${id}/unmatch`, {
        method: "POST",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["matching"] });
    },
  });
}

export function useUnmatchIdentity() {
  const queryClient = useQueryClient();
  return useMutation<ResolveResult, Error, UnmatchVariables>({
    mutationFn: ({ id }) =>
      apiFetch<ResolveResult>(`/api/v1/matching/identities/${id}/unmatch`, {
        method: "POST",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["matching"] });
    },
  });
}

// ---------------------------------------------------------------------------
// MusicBrainz artist search
// ---------------------------------------------------------------------------

export function useMbArtistSearch(query: string) {
  // Normalize on trim so that "prince", "prince ", and "  prince" resolve
  // to the same cache entry and issue a single request per unique term.
  const trimmed = query.trim();
  return useQuery<MbArtistResult[]>({
    queryKey: ["mb-artist-search", trimmed],
    queryFn: async () => {
      const raw = await apiFetch<unknown>(
        `/api/v1/matching/mb-artists?query=${encodeURIComponent(trimmed)}`
      );
      // Parse through the Zod schema so:
      // - `items` defaults to [] if the envelope is missing the field,
      //   honouring the useQuery<MbArtistResult[]> contract.
      // - `disambiguation` defaults to '' per MbArtistResultSchema.
      // - malformed rows surface as a clear ZodError instead of silent
      //   downstream runtime failures.
      return MbArtistSearchResponseSchema.parse(raw).items;
    },
    enabled: trimmed.length > 0,
    staleTime: 30_000,
  });
}

// Mirrors the backend /matching/run response. The button only queues a full re-check
// (spec 2026-10-05 §4.2, D1); results arrive as the matching workers finish.
export interface RunMatchingResponse {
  queued: true;
}

export function useRerunMatching() {
  const queryClient = useQueryClient();
  return useMutation<RunMatchingResponse, Error, void>({
    mutationFn: () =>
      apiFetch<RunMatchingResponse>("/api/v1/matching/run", {
        method: "POST",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["matching"] });
    },
  });
}
