import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiDownload } from "@/api/client";
import type {
  StationList,
  StationResponse,
  StationCreate,
  StationUpdate,
  StationPaginatedEvents,
} from "@/lib/schemas/stations";

const STATIONS_KEY = ["stations"] as const;
const stationKey = (id: string) => ["stations", id] as const;
const stationBroadcastDaysKey = (stationId: string) =>
  ["stations", stationId, "broadcast-days"] as const;
const stationEventsKey = (
  stationId: string,
  date: string,
  limit: number,
  offset: number,
) => ["stations", stationId, "events", { date, limit, offset }] as const;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useStations() {
  return useQuery<StationList>({
    queryKey: STATIONS_KEY,
    queryFn: () => apiFetch<StationList>("/api/v1/stations"),
  });
}

export function useStation(id: string | undefined) {
  return useQuery<StationResponse>({
    queryKey: stationKey(id ?? ""),
    queryFn: () => apiFetch<StationResponse>(`/api/v1/stations/${id}`),
    enabled: Boolean(id),
  });
}

export function useStationBroadcastDays(stationId: string | undefined) {
  return useQuery<string[]>({
    queryKey: stationBroadcastDaysKey(stationId ?? ""),
    queryFn: () =>
      apiFetch<string[]>(`/api/v1/stations/${stationId}/broadcast-days`),
    enabled: Boolean(stationId),
  });
}

export function useStationEvents(
  stationId: string | undefined,
  date: string | undefined,
  limit: number,
  offset: number,
) {
  return useQuery<StationPaginatedEvents>({
    queryKey: stationEventsKey(stationId ?? "", date ?? "", limit, offset),
    queryFn: () =>
      apiFetch<StationPaginatedEvents>(
        `/api/v1/stations/${stationId}/events?date=${date}&limit=${limit}&offset=${offset}`,
      ),
    enabled: Boolean(stationId) && Boolean(date),
  });
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

export function useCreateStation() {
  const qc = useQueryClient();
  return useMutation<StationResponse, Error, StationCreate>({
    mutationFn: (payload) =>
      apiFetch<StationResponse>("/api/v1/stations", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: STATIONS_KEY });
    },
  });
}

export function useUpdateStation(id: string) {
  const qc = useQueryClient();
  return useMutation<StationResponse, Error, StationUpdate>({
    mutationFn: (payload) =>
      apiFetch<StationResponse>(`/api/v1/stations/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: STATIONS_KEY });
      void qc.invalidateQueries({ queryKey: stationKey(id) });
    },
  });
}

export function useDeleteStation() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) =>
      apiFetch<void>(`/api/v1/stations/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: STATIONS_KEY });
    },
  });
}

interface ExportStationM3uVariables {
  stationId: string;
  date: string;
  callLetters: string;
}

export function useExportStationM3u() {
  return useMutation<void, Error, ExportStationM3uVariables>({
    mutationFn: async ({ stationId, date, callLetters }) => {
      const blob = await apiDownload(
        `/api/v1/stations/${stationId}/export-m3u`,
        { date },
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${callLetters}-${date}.m3u`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
  });
}
