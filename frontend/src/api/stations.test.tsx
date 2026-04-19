// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { StationList, StationResponse } from "@/lib/schemas/stations";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
  apiDownload: vi.fn(),
}));

import { apiFetch } from "@/api/client";
import { useDeleteStation } from "./stations";

const STATIONS_KEY = ["stations"] as const;
const stationKey = (id: string) => ["stations", id] as const;
const stationBroadcastDaysKey = (id: string) =>
  ["stations", id, "broadcast-days"] as const;
const stationEventsKey = (
  id: string,
  date: string,
  limit: number,
  offset: number,
) => ["stations", id, "events", { date, limit, offset }] as const;

function makeStation(id: string, call: string): StationResponse & { playlist_count: number } {
  return {
    id,
    call_letters: call,
    name: null,
    city: null,
    format_name: null,
    created_at: "2026-01-01T00:00:00Z",
    playlist_count: 0,
  };
}

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const mockedApiFetch = vi.mocked(apiFetch);

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("useDeleteStation", () => {
  it("optimistically removes the station from the list before the mutation resolves", async () => {
    const qc = makeClient();
    const s1 = makeStation("id-1", "KABC");
    const s2 = makeStation("id-2", "KXYZ");
    qc.setQueryData<StationList>(STATIONS_KEY, [s1, s2]);

    let resolveFetch: (value: void) => void = () => {};
    mockedApiFetch.mockImplementation(
      () => new Promise<void>((resolve) => { resolveFetch = resolve; }) as Promise<never>,
    );

    const { result } = renderHook(() => useDeleteStation(), { wrapper: wrapperFor(qc) });

    act(() => {
      result.current.mutate("id-1");
    });

    await waitFor(() => {
      const list = qc.getQueryData<StationList>(STATIONS_KEY);
      expect(list).toEqual([s2]);
    });

    await act(async () => {
      resolveFetch();
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it("rolls back the list to the original state when the mutation fails", async () => {
    const qc = makeClient();
    const s1 = makeStation("id-1", "KABC");
    const s2 = makeStation("id-2", "KXYZ");
    qc.setQueryData<StationList>(STATIONS_KEY, [s1, s2]);

    mockedApiFetch.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useDeleteStation(), { wrapper: wrapperFor(qc) });

    act(() => {
      result.current.mutate("id-1");
    });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(qc.getQueryData<StationList>(STATIONS_KEY)).toEqual([s1, s2]);
  });

  it("preserves the per-station cache when the mutation fails", async () => {
    const qc = makeClient();
    const s1 = makeStation("id-1", "KABC");
    qc.setQueryData<StationList>(STATIONS_KEY, [s1]);
    qc.setQueryData<StationResponse>(stationKey("id-1"), s1);

    mockedApiFetch.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useDeleteStation(), { wrapper: wrapperFor(qc) });

    act(() => {
      result.current.mutate("id-1");
    });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(qc.getQueryData<StationResponse>(stationKey("id-1"))).toEqual(s1);
  });

  it("clears the per-station, broadcast-days, and events caches on successful delete", async () => {
    const qc = makeClient();
    const s1 = makeStation("id-1", "KABC");
    qc.setQueryData<StationList>(STATIONS_KEY, [s1]);
    qc.setQueryData(stationKey("id-1"), s1);
    qc.setQueryData(stationBroadcastDaysKey("id-1"), ["2026-01-01"]);
    qc.setQueryData(stationEventsKey("id-1", "2026-01-01", 50, 0), {
      items: [],
      total: 0,
    });

    mockedApiFetch.mockResolvedValue(undefined as never);

    const { result } = renderHook(() => useDeleteStation(), { wrapper: wrapperFor(qc) });

    act(() => {
      result.current.mutate("id-1");
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(qc.getQueryData(stationKey("id-1"))).toBeUndefined();
    expect(qc.getQueryData(stationBroadcastDaysKey("id-1"))).toBeUndefined();
    expect(
      qc.getQueryData(stationEventsKey("id-1", "2026-01-01", 50, 0)),
    ).toBeUndefined();
  });

  it("invalidates the stations list on successful delete", async () => {
    const qc = makeClient();
    const s1 = makeStation("id-1", "KABC");
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    qc.setQueryData<StationList>(STATIONS_KEY, [s1]);
    mockedApiFetch.mockResolvedValue(undefined as never);

    const { result } = renderHook(() => useDeleteStation(), { wrapper: wrapperFor(qc) });
    act(() => {
      result.current.mutate("id-1");
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: STATIONS_KEY });
  });

  it("invalidates the stations list on failed delete", async () => {
    const qc = makeClient();
    const s1 = makeStation("id-1", "KABC");
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    qc.setQueryData<StationList>(STATIONS_KEY, [s1]);
    mockedApiFetch.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useDeleteStation(), { wrapper: wrapperFor(qc) });
    act(() => {
      result.current.mutate("id-1");
    });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: STATIONS_KEY });
  });
});
