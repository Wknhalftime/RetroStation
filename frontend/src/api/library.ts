import { useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { useProgressStore } from "@/store/progressStore";
import type { LibraryStatus } from "@/lib/schemas/library";

const LIBRARY_STATUS_KEY = ["library", "status"] as const;

// mb_enrichment is intentionally excluded: it mutates catalog entities
// (Artist/Recording/Work) and only references library_files via existence joins.
// It does not change library_files.enrichment_status, total_files, or
// by_format counts, so the library page has nothing to refresh while it runs.
const SCAN_TYPES = new Set(["scan", "library_enrichment"]);

export function useLibraryStatus() {
  const queryClient = useQueryClient();
  const isScanning = useProgressStore(
    (s) => s.runningTasks.some((t) => SCAN_TYPES.has(t.task_type)),
  );

  useEffect(() => {
    if (!isScanning) return;
    void queryClient.invalidateQueries({ queryKey: LIBRARY_STATUS_KEY });
    const id = setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: LIBRARY_STATUS_KEY });
    }, 2000);
    return () => {
      clearInterval(id);
      void queryClient.invalidateQueries({ queryKey: LIBRARY_STATUS_KEY });
    };
  }, [isScanning, queryClient]);

  return useQuery<LibraryStatus>({
    queryKey: LIBRARY_STATUS_KEY,
    queryFn: () => apiFetch<LibraryStatus>("/api/v1/library/status"),
  });
}

export function useScanLibrary() {
  return useMutation<void, Error, { root_path: string }>({
    mutationFn: (body) =>
      apiFetch<void>("/api/v1/library/scan", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  });
}
