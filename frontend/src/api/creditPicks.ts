import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import {
  ApplyCreditPicksResultSchema,
  CreditPickCountSchema,
  CreditPickListSchema,
  VersionLinePickResultSchema,
  type ApplyCreditPicksResult,
  type CreditPickCount,
  type CreditPickList,
  type VersionLinePickResult,
} from "@/lib/schemas/creditPicks";

// Performances phase 3a PR 3 (S9): the Credit picks page, its count on Library Status, Apply and
// a version line's pick.

const BASE = "/api/v1/library/credit-picks";

export const CREDIT_PICKS_KEY = ["library", "credit-picks"] as const;
const LIST_KEY = [...CREDIT_PICKS_KEY, "list"] as const;
const COUNT_KEY = [...CREDIT_PICKS_KEY, "count"] as const;

// What a credit write can change: both lists and the count (CREDIT_PICKS_KEY prefixes them), and
// the work pages, which show what each line plays.
const AFFECTED_KEYS = [CREDIT_PICKS_KEY, ["works"]] as const;

function useRefresh(): () => void {
  const queryClient = useQueryClient();
  return () => {
    for (const queryKey of AFFECTED_KEYS) void queryClient.invalidateQueries({ queryKey });
  };
}

export function useCreditPicks() {
  return useQuery<CreditPickList>({
    queryKey: LIST_KEY,
    queryFn: async () => CreditPickListSchema.parse(await apiFetch<unknown>(BASE)),
  });
}

export function useCreditPicksCount() {
  return useQuery<CreditPickCount>({
    queryKey: COUNT_KEY,
    queryFn: async () => CreditPickCountSchema.parse(await apiFetch<unknown>(`${BASE}/count`)),
  });
}

export interface ApplyCreditPicksVariables {
  apply: string[];
  skip: string[];
}

export function useApplyCreditPicks() {
  const refresh = useRefresh();
  return useMutation<ApplyCreditPicksResult, Error, ApplyCreditPicksVariables>({
    mutationFn: async (body) =>
      ApplyCreditPicksResultSchema.parse(
        await apiFetch<unknown>(`${BASE}/apply`, { method: "POST", body: JSON.stringify(body) })
      ),
    onSuccess: refresh,
  });
}

export interface PickVersionLineVariables {
  id: string;
  performanceId: string | null;
}

export function usePickVersionLine() {
  const refresh = useRefresh();
  return useMutation<VersionLinePickResult, Error, PickVersionLineVariables>({
    mutationFn: async ({ id, performanceId }) =>
      VersionLinePickResultSchema.parse(
        await apiFetch<unknown>(`${BASE}/${encodeURIComponent(id)}/pick`, {
          method: "PUT",
          body: JSON.stringify({ performance_id: performanceId }),
        })
      ),
    onSuccess: refresh,
  });
}
