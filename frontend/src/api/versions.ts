import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import {
  DismissResultSchema,
  UnmappedVersionPageSchema,
  type DismissResult,
  type UnmappedVersionPage,
} from "@/lib/schemas/versions";

// Versions phase 2b PR 3 (S5, S7): Map, Unmap and Dismiss on a work, the Unmapped versions page
// and its Dismiss selected and Dismiss all.

export const UNMAPPED_VERSIONS_KEY = ["library", "versions"] as const;

// What a version write can change: the work's page, the Unmapped versions page and its count,
// and the artists' new-version counts.
const AFFECTED_KEYS = [["works"], UNMAPPED_VERSIONS_KEY, ["artists"]] as const;

function useRefresh(): () => void {
  const queryClient = useQueryClient();
  return () => {
    for (const queryKey of AFFECTED_KEYS) void queryClient.invalidateQueries({ queryKey });
  };
}

export function useUnmappedVersions(offset: number, limit: number) {
  return useQuery<UnmappedVersionPage>({
    queryKey: [...UNMAPPED_VERSIONS_KEY, offset, limit],
    queryFn: async () =>
      UnmappedVersionPageSchema.parse(
        await apiFetch<unknown>(`/api/v1/library/versions?offset=${offset}&limit=${limit}`)
      ),
  });
}

export interface MapVersionsVariables {
  workId: string;
  versionTexts: string[];
  fileId: string;
}

export function useMapVersions() {
  const refresh = useRefresh();
  return useMutation<void, Error, MapVersionsVariables>({
    mutationFn: ({ workId, versionTexts, fileId }) =>
      apiFetch<void>(`/api/v1/library/works/${workId}/version-entries`, {
        method: "POST",
        body: JSON.stringify({ version_texts: versionTexts, file_id: fileId }),
      }),
    onSuccess: refresh,
  });
}

export interface VersionTextsVariables {
  workId: string;
  versionTexts: string[];
}

export function useUnmapVersions() {
  const refresh = useRefresh();
  return useMutation<void, Error, VersionTextsVariables>({
    mutationFn: ({ workId, versionTexts }) =>
      apiFetch<void>(`/api/v1/library/works/${workId}/version-entries`, {
        method: "DELETE",
        body: JSON.stringify({ version_texts: versionTexts }),
      }),
    onSuccess: refresh,
  });
}

export function useDismissVersions() {
  const refresh = useRefresh();
  return useMutation<void, Error, VersionTextsVariables>({
    mutationFn: ({ workId, versionTexts }) =>
      apiFetch<void>(`/api/v1/library/works/${workId}/versions/dismiss`, {
        method: "POST",
        body: JSON.stringify({ version_texts: versionTexts }),
      }),
    onSuccess: refresh,
  });
}

export type DismissListedBody =
  | { entries: { work_id: string; version_text: string }[] }
  | { all: true }
  | { artist_id: string };

export function useDismissListed() {
  const refresh = useRefresh();
  return useMutation<DismissResult, Error, DismissListedBody>({
    mutationFn: async (body) =>
      DismissResultSchema.parse(
        await apiFetch<unknown>("/api/v1/library/versions/dismiss", {
          method: "POST",
          body: JSON.stringify(body),
        })
      ),
    onSuccess: refresh,
  });
}
