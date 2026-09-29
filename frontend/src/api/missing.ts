import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import {
  MissingFileDeletionSchema,
  MissingFilePageSchema,
  type MissingFileDeletion,
  type MissingFilePage,
} from "@/lib/schemas/missing";

export const MISSING_FILES_KEY = ["library", "missing-files"] as const;
const BASE = "/api/v1/library/missing-files";

export function useMissingFiles(offset: number, limit: number) {
  return useQuery<MissingFilePage>({
    queryKey: [...MISSING_FILES_KEY, offset, limit],
    queryFn: async () =>
      MissingFilePageSchema.parse(
        await apiFetch<unknown>(`${BASE}?offset=${offset}&limit=${limit}`)
      ),
  });
}

export type DeleteMissingFilesBody = { ids: string[] } | { all: true };

function useRefreshLibrary(): () => void {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: MISSING_FILES_KEY });
    void queryClient.invalidateQueries({ queryKey: ["library", "status"] });
  };
}

export function useDeleteMissingFiles() {
  const refresh = useRefreshLibrary();
  return useMutation<MissingFileDeletion, Error, DeleteMissingFilesBody>({
    mutationFn: async (body) =>
      MissingFileDeletionSchema.parse(
        await apiFetch<unknown>(BASE, { method: "DELETE", body: JSON.stringify(body) })
      ),
    onSuccess: refresh,
  });
}

export interface RemapVariables {
  missingId: string;
  targetFileId: string;
}

export function useRemapMissingFile() {
  const refresh = useRefreshLibrary();
  return useMutation<void, Error, RemapVariables>({
    mutationFn: ({ missingId, targetFileId }) =>
      apiFetch<void>(`${BASE}/${missingId}/remap`, {
        method: "POST",
        body: JSON.stringify({ target_file_id: targetFileId }),
      }),
    onSuccess: refresh,
  });
}
