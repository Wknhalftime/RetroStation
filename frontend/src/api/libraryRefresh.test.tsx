// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";
import { useLibraryStatus } from "./library";
import { MISSING_FILES_KEY, useDeleteMissingFiles, useRemapMissingFile } from "./missing";
import { useProgressStore } from "@/store/progressStore";

const mockedApiFetch = vi.mocked(apiFetch);

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

/** The query keys *qc* was asked to invalidate so far. */
function watchInvalidations(qc: QueryClient): () => unknown[] {
  const spy = vi.spyOn(qc, "invalidateQueries");
  return () => spy.mock.calls.map((call) => call[0]?.queryKey);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  useProgressStore.setState({ runningTasks: [] });
});

describe("missing-file mutations", () => {
  it("refreshes the list and the matching queue even when a delete is refused", async () => {
    mockedApiFetch.mockRejectedValue(new Error("library file is no longer missing"));
    const qc = makeClient();
    const invalidated = watchInvalidations(qc);
    const { result } = renderHook(() => useDeleteMissingFiles(), { wrapper: wrapperFor(qc) });

    act(() => result.current.mutate({ ids: ["a"] }));

    await waitFor(() => expect(result.current.isError).toBe(true));
    await waitFor(() =>
      expect(invalidated()).toEqual(
        expect.arrayContaining([MISSING_FILES_KEY, ["matching"], ["library", "status"]])
      )
    );
  });

  it("refreshes the works and artists a remap changed", async () => {
    mockedApiFetch.mockResolvedValue(undefined);
    const qc = makeClient();
    const invalidated = watchInvalidations(qc);
    const { result } = renderHook(() => useRemapMissingFile(), { wrapper: wrapperFor(qc) });

    act(() => result.current.mutate({ missingId: "a", targetFileId: "b" }));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await waitFor(() =>
      expect(invalidated()).toEqual(
        expect.arrayContaining([MISSING_FILES_KEY, ["works"], ["artists"]])
      )
    );
  });

  it("refreshes the list when a remap is refused", async () => {
    mockedApiFetch.mockRejectedValue(new Error("No missing file a"));
    const qc = makeClient();
    const invalidated = watchInvalidations(qc);
    const { result } = renderHook(() => useRemapMissingFile(), { wrapper: wrapperFor(qc) });

    act(() => result.current.mutate({ missingId: "a", targetFileId: "b" }));

    await waitFor(() => expect(result.current.isError).toBe(true));
    await waitFor(() => expect(invalidated()).toEqual(expect.arrayContaining([MISSING_FILES_KEY])));
  });
});

describe("useLibraryStatus during a scan", () => {
  it("refreshes the missing count along with the status", async () => {
    mockedApiFetch.mockResolvedValue({});
    useProgressStore.setState({
      runningTasks: [
        {
          task_id: "t1",
          task_type: "scan",
          status: "running",
          progress_data: {},
          started_at: "2026-09-29T00:00:00Z",
          updated_at: "2026-09-29T00:00:00Z",
          completed_at: null,
        },
      ],
    });
    const qc = makeClient();
    const invalidated = watchInvalidations(qc);

    renderHook(() => useLibraryStatus(), { wrapper: wrapperFor(qc) });

    await waitFor(() =>
      expect(invalidated()).toEqual(
        expect.arrayContaining([["library", "status"], MISSING_FILES_KEY])
      )
    );
  });
});
