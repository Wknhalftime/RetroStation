import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type {
  StationList,
  StationResponse,
  StationCreate,
  StationUpdate,
} from "@/lib/schemas/stations";

const STATIONS_KEY = ["stations"] as const;
const stationKey = (id: string) => ["stations", id] as const;

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
