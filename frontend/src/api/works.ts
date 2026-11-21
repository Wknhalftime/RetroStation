import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ConflictError, apiFetch } from "@/api/client";
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
      // Every work: crowning a file shown from another work moves it off that one.
      void queryClient.invalidateQueries({ queryKey: ["works"] });
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
// Mutations — merge
// ---------------------------------------------------------------------------

export interface MergeWorksResult {
  merged_file_count: number;
  deleted_work_count: number;
  dropped_override_count: number;
}

/** A change to what a line plays that the merge asks about (409), as the server lists it. */
export interface MergeChange {
  station_id: string;
  station: string;
  identity_id: string;
  title: string;
  artist: string;
  plays: number;
  from_file_id: string | null;
  from_path: string | null;
  to_file_id: string | null;
  to_path: string | null;
}

export interface MergeApproval {
  station_id: string;
  identity_id: string;
  file_id: string | null;
}

export interface MergeWorksVariables {
  targetId: string;
  sourceWorkIds: string[];
  /** Lance's approvals; with them the merge goes to merge/approved. */
  approvals?: MergeApproval[];
}

/** The changes a 409 from the merge lists, or null for any other error. */
export function mergeChanges(error: unknown): MergeChange[] | null {
  if (!(error instanceof ConflictError)) return null;
  const detail = error.detail as { changes?: unknown } | undefined;
  return Array.isArray(detail?.changes) ? (detail.changes as MergeChange[]) : null;
}

/** Fold the source works into the target; the sources are deleted. */
export function useMergeWorks() {
  const queryClient = useQueryClient();
  return useMutation<MergeWorksResult, Error, MergeWorksVariables>({
    mutationFn: ({ targetId, sourceWorkIds, approvals }) =>
      approvals
        ? apiFetch<MergeWorksResult>(`/api/v1/library/works/${targetId}/merge/approved`, {
            method: "POST",
            body: JSON.stringify({ source_work_ids: sourceWorkIds, approvals }),
          })
        : apiFetch<MergeWorksResult>(`/api/v1/library/works/${targetId}/merge`, {
            method: "POST",
            body: JSON.stringify({ source_work_ids: sourceWorkIds }),
          }),
    onSuccess: () => {
      // The target gains files and every source work is gone.
      void queryClient.invalidateQueries({ queryKey: ["works"] });
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
      apiFetch<FormatOverrideInfo>(`/api/v1/library/works/${workId}/format-overrides`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
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
      apiFetch<void>(`/api/v1/library/works/${workId}/format-overrides/${oid}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: workKey(workId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Mutations — the work page's copies and pins (3b PR 5a; D41 c, d, e)
// ---------------------------------------------------------------------------

interface PreferCopyVariables {
  performanceId: string;
  fileId: string;
}

/** Prefer this copy: the copy a stored performance's lines play. */
export function usePreferCopy(workId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, Error, PreferCopyVariables>({
    mutationFn: ({ performanceId, fileId }) =>
      apiFetch<void>(
        `/api/v1/library/works/${workId}/performances/${performanceId}/preferred-copy`,
        { method: "PUT", body: JSON.stringify({ file_id: fileId }) }
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: workKey(workId) });
    },
  });
}

/** Release: the lines leave their pins on the work's copies. */
export function useReleasePins(workId: string) {
  const queryClient = useQueryClient();
  return useMutation<{ released: number }, Error, string[]>({
    mutationFn: (identityIds) =>
      apiFetch<{ released: number }>(`/api/v1/library/works/${workId}/pins/release`, {
        method: "POST",
        body: JSON.stringify({ identity_ids: identityIds }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: workKey(workId) });
    },
  });
}
