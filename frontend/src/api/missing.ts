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

// What a delete or a remap can change besides the missing list: the library counts,
// the matching queue (a delete sends identities back to review), and the works and
// artists that show the rows' works (masters re-picked, matches moved, empty works gone).
const AFFECTED_KEYS = [
  MISSING_FILES_KEY,
  ["library", "status"],
  ["matching"],
  ["works"],
  ["artists"],
] as const;

/** Refetches what a delete or remap may have changed, whether it succeeded or was refused. */
function useRefreshLibrary(): () => void {
  const queryClient = useQueryClient();
  return () => {
    for (const queryKey of AFFECTED_KEYS) void queryClient.invalidateQueries({ queryKey });
  };
}

export function useDeleteMissingFiles() {
  const refresh = useRefreshLibrary();
  return useMutation<MissingFileDeletion, Error, DeleteMissingFilesBody>({
    mutationFn: async (body) =>
      MissingFileDeletionSchema.parse(
        await apiFetch<unknown>(BASE, { method: "DELETE", body: JSON.stringify(body) })
      ),
    onSettled: refresh,
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
    onSettled: refresh,
  });
}
