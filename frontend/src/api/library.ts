import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { LibraryStatus } from "@/lib/schemas/library";

const LIBRARY_STATUS_KEY = ["library", "status"] as const;

export function useLibraryStatus() {
  return useQuery<LibraryStatus>({
    queryKey: LIBRARY_STATUS_KEY,
    queryFn: () => apiFetch<LibraryStatus>("/api/v1/library/status"),
  });
}
