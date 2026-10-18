import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch, apiFetchBytes, apiUpload } from "@/api/client";
import {
  CueEditorSchema,
  WaveformSchema,
  type ApiMarkers,
  type CueEditor,
  type Waveform,
} from "@/lib/schemas/cues";
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
export const linerCuesKey = (linerId: string) => ["liner-cues", linerId] as const;
export const linerWaveformKey = (linerId: string) => ["liner-waveform", linerId] as const;
export const linerCueAudioKey = (linerId: string) => ["liner-cue-audio", linerId] as const;

const linersPath = (stationId: string) => `/api/v1/stations/${stationId}/liners`;
const linerPath = (stationId: string, linerId: string) => `${linersPath(stationId)}/${linerId}`;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useStationLiners(stationId: string) {
  return useQuery<LinerLibrary>({
    queryKey: stationLinersKey(stationId),
    queryFn: async () => LinerLibrarySchema.parse(await apiFetch(linersPath(stationId))),
  });
}

// The stored file, for the tab's preview (GET .../liners/{liner_id}/audio). The bytes come
// through the shared apiFetchBytes and are wrapped in an untyped Blob for the tab's object
// URL; the audio element sniffs the format from the bytes.
export async function fetchLinerAudio(stationId: string, linerId: string): Promise<Blob> {
  return new Blob([await apiFetchBytes(`${linerPath(stationId, linerId)}/audio`)]);
}

// ---------------------------------------------------------------------------
// The cue editor on a liner: the same shapes as the library editor's
// ---------------------------------------------------------------------------

export function useLinerCues(stationId: string, linerId: string) {
  return useQuery<CueEditor>({
    queryKey: linerCuesKey(linerId),
    queryFn: async () =>
      CueEditorSchema.parse(await apiFetch(`${linerPath(stationId, linerId)}/cues`)),
    // A 404 is the answer (no such liner on this station): show it at once.
    retry: (failures, error) => !(error instanceof ApiError && error.status < 500) && failures < 2,
  });
}

// Peaks never change for a stored liner, so they are fetched once and kept.
export function useLinerWaveform(stationId: string, linerId: string) {
  return useQuery<Waveform>({
    queryKey: linerWaveformKey(linerId),
    queryFn: async () =>
      WaveformSchema.parse(await apiFetch(`${linerPath(stationId, linerId)}/waveform`)),
    staleTime: Infinity,
  });
}

// The stored liner for the editor's preview: fetched once per visit, cancelled if the page is
// left first, and dropped from the cache as soon as nothing uses it (as useCueAudio).
export function useLinerCueAudio(stationId: string, linerId: string) {
  return useQuery<ArrayBuffer>({
    queryKey: linerCueAudioKey(linerId),
    queryFn: ({ signal }) => apiFetchBytes(`${linerPath(stationId, linerId)}/audio`, signal),
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
}

/** After a save or reset: the editor reads the liner again, and the tab's Cues column too. */
function useRefreshLinerCues(stationId: string, linerId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: linerCuesKey(linerId) });
    void queryClient.invalidateQueries({ queryKey: stationLinersKey(stationId) });
  };
}

// The liner API takes no fade (a liner's Fade-out is always its Stop): it is never sent.
export function useSaveLinerCues(stationId: string, linerId: string) {
  const refresh = useRefreshLinerCues(stationId, linerId);
  return useMutation<void, Error, ApiMarkers>({
    mutationFn: ({ start_ms, eom_ms, stop_ms }) =>
      apiFetch<void>(`${linerPath(stationId, linerId)}/cues`, {
        method: "PUT",
        body: JSON.stringify({ start_ms, eom_ms, stop_ms }),
      }),
    onSuccess: refresh,
  });
}

export function useResetLinerCues(stationId: string, linerId: string) {
  const refresh = useRefreshLinerCues(stationId, linerId);
  return useMutation<void, Error, void>({
    mutationFn: () => apiFetch<void>(`${linerPath(stationId, linerId)}/cues`, { method: "DELETE" }),
    onSuccess: refresh,
  });
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
