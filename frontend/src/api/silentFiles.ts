import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { SilentFileReportSchema, type SilentFileReport } from "@/lib/schemas/silentFiles";

export const SILENT_FILES_KEY = ["library", "silent-files"] as const;

export function useSilentFiles() {
  return useQuery<SilentFileReport>({
    queryKey: SILENT_FILES_KEY,
    queryFn: async () =>
      SilentFileReportSchema.parse(await apiFetch<unknown>("/api/v1/library/silent-files")),
  });
}
