import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { PaginatedArtists, ArtistDetail } from "@/lib/schemas/artists";

const PAGE_SIZE = 50;

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
