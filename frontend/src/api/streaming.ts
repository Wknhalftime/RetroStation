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

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

export const streamSettingsKey = ["streaming-settings"] as const;

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
