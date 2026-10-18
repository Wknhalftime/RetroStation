import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiGetBlob, apiUpload } from "@/api/client";
import {
  LinerAddedSchema,
  LinerLibrarySchema,
  type LinerAdded,
  type LinerLibrary,
} from "@/lib/schemas/liners";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

export const stationLinersKey = (stationId: string) => ["station-liners", stationId] as const;

const linersPath = (stationId: string) => `/api/v1/stations/${stationId}/liners`;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useStationLiners(stationId: string) {
  return useQuery<LinerLibrary>({
    queryKey: stationLinersKey(stationId),
    queryFn: async () => LinerLibrarySchema.parse(await apiFetch(linersPath(stationId))),
  });
}

// The stored file, for the tab's preview (GET .../liners/{liner_id}/audio).
export function fetchLinerAudio(stationId: string, linerId: string): Promise<Blob> {
  return apiGetBlob(`${linersPath(stationId)}/${linerId}/audio`);
}

// ---------------------------------------------------------------------------
// Mutations — each refreshes the station's liner list
// ---------------------------------------------------------------------------

function useRefreshLiners(stationId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: stationLinersKey(stationId) });
  };
}

// null turns liners off.
export function useSetLinerCadence(stationId: string) {
  const refresh = useRefreshLiners(stationId);
  return useMutation<LinerLibrary, Error, number | null>({
    mutationFn: async (songs) =>
      LinerLibrarySchema.parse(
        await apiFetch(`${linersPath(stationId)}/cadence`, {
          method: "PUT",
          body: JSON.stringify({ songs_between_liners: songs }),
        })
      ),
    onSuccess: refresh,
  });
}

// One file per call: the tab sends a folder's files one request at a time.
export function useUploadLiner(stationId: string) {
  const refresh = useRefreshLiners(stationId);
  return useMutation<LinerAdded, Error, File>({
    mutationFn: async (file) => {
      const formData = new FormData();
      formData.append("file", file);
      return LinerAddedSchema.parse(await apiUpload(linersPath(stationId), formData));
    },
    onSuccess: refresh,
  });
}

export function useRenameLiner(stationId: string) {
  const refresh = useRefreshLiners(stationId);
  return useMutation<void, Error, { id: string; name: string }>({
    mutationFn: ({ id, name }) =>
      apiFetch<void>(`${linersPath(stationId)}/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    onSuccess: refresh,
  });
}

export function useDeleteLiner(stationId: string) {
  const refresh = useRefreshLiners(stationId);
  return useMutation<void, Error, string>({
    mutationFn: (id) => apiFetch<void>(`${linersPath(stationId)}/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
}
