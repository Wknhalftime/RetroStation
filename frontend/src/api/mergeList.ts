import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import {
  ApplyMergeListResultSchema,
  MergeListCountSchema,
  MergeListSchema,
  type ApplyMergeListResult,
  type MergeList,
  type MergeListCount,
} from "@/lib/schemas/mergeList";

// Work merges phase 3b PR 4 (S2, D6, D28): the merge list, its count on Library Status, Apply.

const BASE = "/api/v1/library/merge-list";

export const MERGE_LIST_KEY = ["library", "merge-list"] as const;
const LIST_KEY = [...MERGE_LIST_KEY, "list"] as const;
const COUNT_KEY = [...MERGE_LIST_KEY, "count"] as const;

// A merge changes the list and its count, the work and artist pages, and Credit picks.
const AFFECTED_KEYS = [
  MERGE_LIST_KEY,
  ["works"],
  ["artists"],
  ["library", "credit-picks"],
] as const;

// D37: 50 groups a page.
export const MERGE_LIST_PAGE = 50;

export function useMergeList(offset: number) {
  return useQuery<MergeList>({
    queryKey: [...LIST_KEY, offset],
    queryFn: async () =>
      MergeListSchema.parse(
        await apiFetch<unknown>(`${BASE}?offset=${offset}&limit=${MERGE_LIST_PAGE}`)
      ),
  });
}

export function useMergeListCount() {
  return useQuery<MergeListCount>({
    queryKey: COUNT_KEY,
    queryFn: async () => MergeListCountSchema.parse(await apiFetch<unknown>(`${BASE}/count`)),
  });
}

export interface SeenMergeGroup {
  target_id: string;
  source_ids: string[];
  approvals: { station_id: string; identity_id: string; file_id: string | null }[];
}

export function useApplyMergeList() {
  const queryClient = useQueryClient();
  return useMutation<ApplyMergeListResult, Error, SeenMergeGroup[]>({
    mutationFn: async (groups) =>
      ApplyMergeListResultSchema.parse(
        await apiFetch<unknown>(`${BASE}/apply`, {
          method: "POST",
          body: JSON.stringify({ groups }),
        })
      ),
    onSuccess: () => {
      for (const queryKey of AFFECTED_KEYS) void queryClient.invalidateQueries({ queryKey });
    },
  });
}
