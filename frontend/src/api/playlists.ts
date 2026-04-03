import { useQuery, useMutation } from "@tanstack/react-query";
import { apiFetch, apiDownload } from "@/api/client";
import type { PlaylistSummary, PaginatedEvents } from "@/lib/schemas/playlists";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

const playlistsKey = (stationId: string) =>
  ["playlists", { stationId }] as const;

const playlistEventsKey = (
  playlistId: string,
  limit: number,
  offset: number,
) => ["playlists", playlistId, "events", { limit, offset }] as const;

const broadcastDaysKey = (playlistId: string) =>
  ["playlists", playlistId, "broadcast-days"] as const;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function usePlaylists(stationId: string | undefined) {
  return useQuery<PlaylistSummary[]>({
    queryKey: playlistsKey(stationId ?? ""),
    queryFn: () =>
      apiFetch<PlaylistSummary[]>(
        `/api/v1/playlists?station_id=${stationId}`,
      ),
    enabled: Boolean(stationId),
  });
}

export function usePlaylistEvents(
  playlistId: string | undefined,
  limit: number,
  offset: number,
) {
  return useQuery<PaginatedEvents>({
    queryKey: playlistEventsKey(playlistId ?? "", limit, offset),
    queryFn: () =>
      apiFetch<PaginatedEvents>(
        `/api/v1/playlists/${playlistId}/events?limit=${limit}&offset=${offset}`,
      ),
    enabled: Boolean(playlistId),
  });
}

export function useBroadcastDays(playlistId: string | undefined) {
  return useQuery<string[]>({
    queryKey: broadcastDaysKey(playlistId ?? ""),
    queryFn: () =>
      apiFetch<string[]>(`/api/v1/playlists/${playlistId}/broadcast-days`),
    enabled: Boolean(playlistId),
  });
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

interface ExportM3uVariables {
  playlistId: string;
  formatName?: string | null;
}

export function useExportM3u() {
  return useMutation<void, Error, ExportM3uVariables>({
    mutationFn: async ({ playlistId, formatName }) => {
      const blob = await apiDownload(
        `/api/v1/playlists/${playlistId}/export-m3u`,
        formatName ? { format_name: formatName } : undefined,
      );
      // Trigger browser download
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `playlist-${playlistId}.m3u`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
  });
}
