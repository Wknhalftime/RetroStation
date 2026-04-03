import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { SettingsMap } from "@/lib/schemas/settings";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

const settingsKey = ["settings"] as const;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useSettings() {
  return useQuery<SettingsMap>({
    queryKey: settingsKey,
    queryFn: () => apiFetch<SettingsMap>("/api/v1/settings"),
  });
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

interface UpdateSettingVariables {
  key: string;
  value: string;
}

export function useUpdateSetting() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, UpdateSettingVariables>({
    mutationFn: ({ key, value }) =>
      apiFetch<void>(`/api/v1/settings/${encodeURIComponent(key)}`, {
        method: "PUT",
        body: JSON.stringify({ value }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}
