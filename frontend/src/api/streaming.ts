import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiUpload } from "@/api/client";
import {
  StreamSettingsSchema,
  MaxSessionsResponseSchema,
  SignOffSchema,
  type StreamSettings,
  type MaxSessionsResponse,
  type SignOff,
} from "@/lib/schemas/streaming";
import { CueCoverageSchema, type CueCoverage } from "@/lib/schemas/streamingMeters";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

export const streamSettingsKey = ["streaming-settings"] as const;
export const cueCoverageKey = ["streaming-cue-coverage"] as const;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useStreamSettings() {
  return useQuery<StreamSettings>({
    queryKey: streamSettingsKey,
    queryFn: async () => {
      const data = await apiFetch("/api/v1/streaming/settings");
      return StreamSettingsSchema.parse(data);
    },
  });
}

// GET /api/v1/streaming/cue-coverage (G2, D77/PG7). It opens a fresh database
// connection per request (the count scans the whole library), so this is
// fetched once on page load and refetched when a cue run completes (VG3) —
// never on an interval.
export function useCueCoverage() {
  return useQuery<CueCoverage>({
    queryKey: cueCoverageKey,
    queryFn: async () => {
      const data = await apiFetch("/api/v1/streaming/cue-coverage");
      return CueCoverageSchema.parse(data);
    },
    refetchOnWindowFocus: false,
  });
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

export function useSetMaxSessions() {
  const queryClient = useQueryClient();
  return useMutation<MaxSessionsResponse, Error, number>({
    mutationFn: async (value) => {
      const data = await apiFetch("/api/v1/streaming/max-sessions", {
        method: "PUT",
        body: JSON.stringify({ value }),
      });
      return MaxSessionsResponseSchema.parse(data);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: streamSettingsKey });
    },
  });
}

export function useUploadSignOff() {
  const queryClient = useQueryClient();
  return useMutation<SignOff, Error, File>({
    mutationFn: async (file) => {
      const formData = new FormData();
      formData.append("file", file);
      const data = await apiUpload("/api/v1/streaming/sign-off", formData);
      return SignOffSchema.parse(data);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: streamSettingsKey });
    },
  });
}

export function useDeleteSignOff() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () => apiFetch<void>("/api/v1/streaming/sign-off", { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: streamSettingsKey });
    },
  });
}
