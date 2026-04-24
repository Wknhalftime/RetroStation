// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "@/api/client";
import { useMbArtistSearch } from "./matcher";

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

describe("useMbArtistSearch", () => {
  it("stays disabled and skips fetch when query is empty", async () => {
    const qc = makeClient();
    renderHook(() => useMbArtistSearch(""), { wrapper: wrapperFor(qc) });
    // Allow any microtasks to flush
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it("stays disabled and skips fetch when query is whitespace-only", async () => {
    const qc = makeClient();
    renderHook(() => useMbArtistSearch("   "), { wrapper: wrapperFor(qc) });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it("fetches with the trimmed query so padding variants share a cache entry", async () => {
    mockedApiFetch.mockResolvedValue({ items: [] });
    const qc = makeClient();
    const wrapper = wrapperFor(qc);

    // Three renders with different padding but the same logical query.
    renderHook(() => useMbArtistSearch("prince"), { wrapper });
    renderHook(() => useMbArtistSearch(" prince "), { wrapper });
    renderHook(() => useMbArtistSearch("  prince"), { wrapper });

    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalled();
    });

    // Exactly one fetch total — cache key normalization collapses padding
    // variants onto the same entry.
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
    expect(mockedApiFetch).toHaveBeenCalledWith(expect.stringContaining("query=prince"));
  });

  it("returns the items array from the backend envelope", async () => {
    mockedApiFetch.mockResolvedValue({
      items: [{ id: "mbid-1", name: "Prince", score: 100, disambiguation: "" }],
    });
    const qc = makeClient();
    const { result } = renderHook(() => useMbArtistSearch("prince"), {
      wrapper: wrapperFor(qc),
    });

    await waitFor(() => {
      expect(result.current.data).toBeDefined();
    });

    expect(result.current.data).toEqual([
      { id: "mbid-1", name: "Prince", score: 100, disambiguation: "" },
    ]);
  });

  it("defaults items to [] when the envelope omits the field", async () => {
    // Schema coerces missing `items` to [] so useQuery<MbArtistResult[]>
    // never surfaces `undefined` to consumers that assume an array.
    mockedApiFetch.mockResolvedValue({});
    const qc = makeClient();
    const { result } = renderHook(() => useMbArtistSearch("prince"), {
      wrapper: wrapperFor(qc),
    });
    await waitFor(() => {
      expect(result.current.data).toBeDefined();
    });
    expect(result.current.data).toEqual([]);
  });

  it("applies the disambiguation default when the field is missing", async () => {
    mockedApiFetch.mockResolvedValue({
      items: [{ id: "mbid-1", name: "Prince", score: 100 }],
    });
    const qc = makeClient();
    const { result } = renderHook(() => useMbArtistSearch("prince"), {
      wrapper: wrapperFor(qc),
    });
    await waitFor(() => {
      expect(result.current.data).toBeDefined();
    });
    expect(result.current.data?.[0]?.disambiguation).toBe("");
  });
});
