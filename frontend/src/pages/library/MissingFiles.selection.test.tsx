// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";
import { MissingFiles } from "./MissingFiles";
import { deletionPrompt } from "@/components/domain/library/deletionPrompt";

const mockedApiFetch = vi.mocked(apiFetch);
const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const X = "55555555-5555-4555-8555-555555555555";

const row = (id: string, path: string, matches: number) => ({
  id,
  file_path: path,
  artist_name: "Prince",
  track_title: "Kiss",
  release_title: "Parade",
  missing_since: "2026-09-27T10:00:00Z",
  work_id: "w1",
  work_title: "Kiss",
  match_count: matches,
  work_has_present_file: true,
  candidates: [],
});

const ROW_A = row(A, "D:\\Music\\old\\kiss.flac", 2);
const ROW_B = row(B, "D:\\Music\\old\\snow.flac", 1);
const ROW_X = row(X, "D:\\Music\\old\\last.flac", 1);

let qc: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function deleteCalls(): [string, RequestInit][] {
  return mockedApiFetch.mock.calls.filter(
    ([, init]) => (init as RequestInit | undefined)?.method === "DELETE"
  ) as [string, RequestInit][];
}

function pageRequests(offset: number): number {
  return mockedApiFetch.mock.calls.filter(([url]) =>
    url.startsWith(`/api/v1/library/missing-files?offset=${offset}&`)
  ).length;
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  mockedApiFetch.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MissingFiles selection follows the rows on screen", () => {
  it("drops a selected row that a refetch removed from the prompt and the request", async () => {
    let items = [ROW_A, ROW_B];
    mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "DELETE")
        return Promise.resolve({ deleted: 1, matches_released: 1, skipped: 0 });
      if (url.startsWith("/api/v1/library/missing-files?"))
        return Promise.resolve({ items, total: items.length, total_match_count: 3 });
      return Promise.resolve({});
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MissingFiles />, { wrapper });

    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Select every file on this page" })
    );
    items = [ROW_B];
    await qc.invalidateQueries();
    await waitFor(() => expect(screen.queryByRole("checkbox", { name: /kiss\.flac/ })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(confirm).toHaveBeenCalledWith(deletionPrompt(1, 1));
    await waitFor(() => expect(deleteCalls()).toHaveLength(1));
    expect(JSON.parse(deleteCalls()[0][1].body as string)).toEqual({ ids: [B] });
  });

  it("disables Delete selected when every selected row has gone", async () => {
    let items = [ROW_A, ROW_B];
    mockedApiFetch.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/library/missing-files?"))
        return Promise.resolve({ items, total: items.length, total_match_count: 3 });
      return Promise.resolve({});
    });
    render(<MissingFiles />, { wrapper });

    fireEvent.click(await screen.findByRole("checkbox", { name: /kiss\.flac/ }));
    items = [ROW_B];
    await qc.invalidateQueries();
    await waitFor(() => expect(screen.queryByRole("checkbox", { name: /kiss\.flac/ })).toBeNull());

    const button = screen.getByRole("button", { name: "Delete selected" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });
});

describe("MissingFiles when a page empties", () => {
  it("steps back to the last page instead of claiming nothing is missing", async () => {
    let lastPageDeleted = false;
    mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        lastPageDeleted = true;
        return Promise.resolve({ deleted: 1, matches_released: 1, skipped: 0 });
      }
      if (url.startsWith("/api/v1/library/missing-files?offset=50&"))
        return Promise.resolve(
          lastPageDeleted
            ? { items: [], total: 50, total_match_count: 3 }
            : { items: [ROW_X], total: 51, total_match_count: 4 }
        );
      if (url.startsWith("/api/v1/library/missing-files?offset=0&"))
        return Promise.resolve({
          items: [ROW_A, ROW_B],
          total: lastPageDeleted ? 50 : 51,
          total_match_count: 3,
        });
      return Promise.resolve({});
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MissingFiles />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /last\.flac/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    await waitFor(() => expect(pageRequests(0)).toBeGreaterThanOrEqual(2));
    expect(await screen.findByRole("checkbox", { name: /kiss\.flac/ })).toBeTruthy();
    expect(screen.queryByText("No missing files")).toBeNull();
  });

  it("says nothing is missing only when the total is zero", async () => {
    mockedApiFetch.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/library/missing-files?"))
        return Promise.resolve({ items: [], total: 0, total_match_count: 0 });
      return Promise.resolve({});
    });
    render(<MissingFiles />, { wrapper });

    expect(await screen.findByText("No missing files")).toBeTruthy();
  });
});
