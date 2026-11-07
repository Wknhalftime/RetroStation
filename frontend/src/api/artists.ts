import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { PaginatedArtists, ArtistDetail } from "@/lib/schemas/artists";

const PAGE_SIZE = 50;
const MBID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// ---------------------------------------------------------------------------
// Infinite query — artist browser
// ---------------------------------------------------------------------------

export function useArtistsInfinite(search?: string) {
  return useInfiniteQuery<PaginatedArtists>({
    queryKey: ["artists", "infinite", search ?? ""],
    queryFn: ({ pageParam }) => {
      const offset = (pageParam as number) ?? 0;
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (search) params.set("search", search);
      return apiFetch<PaginatedArtists>(`/api/v1/library/artists?${params}`);
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const fetched = allPages.reduce((sum, p) => sum + p.items.length, 0);
      return fetched < lastPage.total ? fetched : undefined;
    },
  });
}

// ---------------------------------------------------------------------------
// Single artist detail
// ---------------------------------------------------------------------------

export function useArtistDetail(artistId: string | undefined) {
  return useQuery<ArtistDetail>({
    queryKey: ["artists", artistId],
    queryFn: () => apiFetch<ArtistDetail>(`/api/v1/library/artists/${artistId}`),
    enabled: Boolean(artistId),
  });
}

// ---------------------------------------------------------------------------
// Merge and Unmerge (phase 2a S11): the picker's search, and the two writes
// ---------------------------------------------------------------------------

// The Merge picker's search: an MBID looks the artist up by it (a merged member's MBID finds its
// primary), anything else searches names.
export function useArtistPick(query: string) {
  const text = query.trim();
  return useQuery<PaginatedArtists>({
    queryKey: ["artists", "pick", text],
    queryFn: () => {
      const params = new URLSearchParams({ limit: "20" });
      params.set(MBID.test(text) ? "mbid" : "search", text);
      return apiFetch<PaginatedArtists>(`/api/v1/library/artists?${params}`);
    },
    enabled: text.length > 0,
  });
}

export interface MergeVariables {
  artistId: string;
  primaryId: string;
  reason?: string;
}

export function useMergeArtist() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, MergeVariables>({
    mutationFn: ({ artistId, primaryId, reason }) =>
      apiFetch<void>(`/api/v1/library/artists/${artistId}/merge`, {
        method: "POST",
        body: JSON.stringify({ primary_id: primaryId, reason: reason || null }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["artists"] }),
  });
}

export interface UnmergeVariables {
  artistId: string;
  // The artist and every member that leaves with it: their cached pages are dropped.
  leaving: string[];
}

export function useUnmergeArtist() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, UnmergeVariables>({
    mutationFn: ({ artistId }) =>
      apiFetch<void>(`/api/v1/library/artists/${artistId}/merge`, { method: "DELETE" }),
    onSuccess: (_data, { leaving }) => {
      // A page cached while merged would redirect again: drop it, so it loads afresh.
      for (const id of leaving) queryClient.removeQueries({ queryKey: ["artists", id] });
      return queryClient.invalidateQueries({ queryKey: ["artists"] });
    },
  });
}
