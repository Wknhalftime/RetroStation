import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiFetchBytes } from "@/api/client";
import {
  CueEditorSchema,
  WaveformSchema,
  type ApiMarkers,
  type CueEditor,
  type Waveform,
} from "@/lib/schemas/cues";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

export const cueEditorKey = (fileId: string) => ["cue-editor", fileId] as const;
export const cueWaveformKey = (fileId: string) => ["cue-waveform", fileId] as const;

const filePath = (fileId: string) => `/api/v1/library/files/${fileId}`;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useCueEditor(fileId: string) {
  return useQuery<CueEditor>({
    queryKey: cueEditorKey(fileId),
    queryFn: async () => {
      const data = await apiFetch(`${filePath(fileId)}/cues`);
      return CueEditorSchema.parse(data);
    },
  });
}

// Peaks never change for a file, so they are fetched once and kept.
export function useWaveform(fileId: string) {
  return useQuery<Waveform>({
    queryKey: cueWaveformKey(fileId),
    queryFn: async () => {
      const data = await apiFetch(`${filePath(fileId)}/waveform`);
      return WaveformSchema.parse(data);
    },
    staleTime: Infinity,
  });
}

// The song's file, for the browser to decode and play.
export function fetchAudio(fileId: string): Promise<ArrayBuffer> {
  return apiFetchBytes(`${filePath(fileId)}/audio`);
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

export function useSaveCues(fileId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, Error, ApiMarkers>({
    mutationFn: (markers) =>
      apiFetch<void>(`${filePath(fileId)}/cues`, {
        method: "PUT",
        body: JSON.stringify(markers),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: cueEditorKey(fileId) });
    },
  });
}

export function useResetCues(fileId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () => apiFetch<void>(`${filePath(fileId)}/cues`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: cueEditorKey(fileId) });
    },
  });
}
