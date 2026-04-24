import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import {
  SystemLogPageSchema,
  SystemLogEntrySchema,
  type SystemLogPage,
  type SystemLogEntry,
} from "@/lib/schemas/system_logs";
import { z } from "zod";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

export const systemLogsKeys = {
  all: ["system-logs"] as const,
  list: (params: SystemLogsParams) => ["system-logs", "list", params] as const,
  byTrace: (traceId: string) => ["system-logs", "trace", traceId] as const,
};

// ---------------------------------------------------------------------------
// Params
// ---------------------------------------------------------------------------

export interface SystemLogsParams {
  level?: string;
  category?: string;
  trace_id?: string;
  limit?: number;
  offset?: number;
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function useSystemLogs(
  params: SystemLogsParams = {}
): ReturnType<typeof useQuery<SystemLogPage>> {
  const { level, category, trace_id, limit = 100, offset = 0 } = params;

  return useQuery<SystemLogPage>({
    queryKey: systemLogsKeys.list(params),
    queryFn: async () => {
      const qs = new URLSearchParams();
      if (level) qs.set("level", level);
      if (category) qs.set("category", category);
      if (trace_id) qs.set("trace_id", trace_id);
      qs.set("limit", String(limit));
      qs.set("offset", String(offset));

      const data = await apiFetch(`/api/v1/system-logs?${qs.toString()}`);
      return SystemLogPageSchema.parse(data);
    },
    staleTime: 30_000,
  });
}

export function useSystemLogsByTrace(
  traceId: string
): ReturnType<typeof useQuery<SystemLogEntry[]>> {
  return useQuery<SystemLogEntry[]>({
    queryKey: systemLogsKeys.byTrace(traceId),
    queryFn: async () => {
      const data = await apiFetch(`/api/v1/system-logs/by-trace/${traceId}`);
      return z.array(SystemLogEntrySchema).parse(data);
    },
    enabled: Boolean(traceId),
    staleTime: 30_000,
  });
}
