import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { WorkDetail, FormatOverrideInfo } from "@/lib/schemas/works";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

const workKey = (workId: string) => ["works", workId] as const;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useWorkDetail(workId: string | undefined) {
  return useQuery<WorkDetail>({
    queryKey: workKey(workId ?? ""),
    queryFn: () => apiFetch<WorkDetail>(`/api/v1/library/works/${workId}`),
    enabled: Boolean(workId),
  });
}

// ---------------------------------------------------------------------------
// Mutations — master
// ---------------------------------------------------------------------------

interface SetMasterVariables {
  preferred_file_id: string;
}

export function useSetMaster(workId: string) {
  const queryClient = useQueryClient();
  return useMutation<WorkDetail, Error, SetMasterVariables>({
    mutationFn: (body) =>
      apiFetch<WorkDetail>(`/api/v1/library/works/${workId}/master`, {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: workKey(workId) });
      void queryClient.invalidateQueries({ queryKey: ["artists"] });
    },
  });
}

export function useRevertMaster(workId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>(`/api/v1/library/works/${workId}/master`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: workKey(workId) });
      void queryClient.invalidateQueries({ queryKey: ["artists"] });
    },
  });
}

// ---------------------------------------------------------------------------
// Mutations — format overrides
// ---------------------------------------------------------------------------

interface CreateFormatOverrideVariables {
  format_name: string;
  preferred_file_id: string;
  notes?: string | null;
}

export function useCreateFormatOverride(workId: string) {
  const queryClient = useQueryClient();
  return useMutation<FormatOverrideInfo, Error, CreateFormatOverrideVariables>({
    mutationFn: (body) =>
      apiFetch<FormatOverrideInfo>(
        `/api/v1/library/works/${workId}/format-overrides`,
        {
          method: "POST",
          body: JSON.stringify(body),
        },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: workKey(workId) });
    },
  });
}

interface DeleteFormatOverrideVariables {
  oid: string;
}

export function useDeleteFormatOverride(workId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, Error, DeleteFormatOverrideVariables>({
    mutationFn: ({ oid }) =>
      apiFetch<void>(
        `/api/v1/library/works/${workId}/format-overrides/${oid}`,
        { method: "DELETE" },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: workKey(workId) });
    },
  });
}
