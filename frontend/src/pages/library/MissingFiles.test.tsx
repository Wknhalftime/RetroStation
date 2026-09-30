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
const COPY = "33333333-3333-4333-8333-333333333333";

const row = (
  id: string,
  path: string,
  matches: number,
  candidates: { id: string; file_path: string }[]
) => ({
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
  candidates,
});

const PAGE = {
  items: [
    row(A, "D:\\Music\\old\\kiss.flac", 2, [{ id: COPY, file_path: "D:\\Music\\new\\kiss.flac" }]),
    row(B, "D:\\Music\\old\\snow.flac", 1, []),
  ],
  total: 3,
  total_match_count: 4,
};

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function callsWith(method: string): [string, RequestInit][] {
  return mockedApiFetch.mock.calls.filter(
    ([, init]) => (init as RequestInit | undefined)?.method === method
  ) as [string, RequestInit][];
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method === "DELETE")
      return Promise.resolve({ deleted: 1, matches_released: 2, skipped: 0 });
    if (url.startsWith("/api/v1/library/missing-files?")) return Promise.resolve(PAGE);
    if (url === "/api/v1/settings") return Promise.resolve({});
    return Promise.resolve(undefined);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MissingFiles", () => {
  it("asks before deleting the selection, naming the matches it releases", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<MissingFiles />, { wrapper });

    fireEvent.click(await screen.findByRole("checkbox", { name: /old\\kiss\.flac/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(confirm).toHaveBeenCalledWith(deletionPrompt(1, 2));
    expect(callsWith("DELETE")).toHaveLength(0);
  });

  it("deletes the selected rows by id once confirmed, and says what was released", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MissingFiles />, { wrapper });

    fireEvent.click(await screen.findByRole("checkbox", { name: /old\\kiss\.flac/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    await waitFor(() => expect(callsWith("DELETE")).toHaveLength(1));
    const [url, init] = callsWith("DELETE")[0];
    expect(url).toBe("/api/v1/library/missing-files");
    expect(JSON.parse(init.body as string)).toEqual({ ids: [A] });
    expect((await screen.findByRole("status")).textContent).toContain("2 matches released");
  });

  it("selects every row of the page and deletes them by id", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MissingFiles />, { wrapper });

    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Select every file on this page" })
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(confirm).toHaveBeenCalledWith(deletionPrompt(2, 3));
    await waitFor(() => expect(callsWith("DELETE")).toHaveLength(1));
    const body = JSON.parse(callsWith("DELETE")[0][1].body as string) as { ids: string[] };
    expect([...body.ids].sort()).toEqual([A, B].sort());
  });

  it("confirms Delete all with the totals over every missing file", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MissingFiles />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Delete all" }));

    expect(confirm).toHaveBeenCalledWith(deletionPrompt(3, 4));
    await waitFor(() => expect(callsWith("DELETE")).toHaveLength(1));
    expect(JSON.parse(callsWith("DELETE")[0][1].body as string)).toEqual({ all: true });
  });

  it("remaps a row onto one of its candidates", async () => {
    render(<MissingFiles />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Remap (1)" }));
    fireEvent.click(screen.getByRole("button", { name: "Use this file" }));

    await waitFor(() => expect(callsWith("POST")).toHaveLength(1));
    const [url, init] = callsWith("POST")[0];
    expect(url).toBe(`/api/v1/library/missing-files/${A}/remap`);
    expect(JSON.parse(init.body as string)).toEqual({ target_file_id: COPY });
  });

  it("remaps a row onto a file found by the library search", async () => {
    const FOUND = "44444444-4444-4444-8444-444444444444";
    mockedApiFetch.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/library/files?"))
        return Promise.resolve({
          items: [{ id: FOUND, file_path: "D:\\Music\\found\\kiss.flac", track_title: "Kiss" }],
        });
      if (url.startsWith("/api/v1/library/missing-files?")) return Promise.resolve(PAGE);
      if (url === "/api/v1/settings") return Promise.resolve({});
      return Promise.resolve(undefined);
    });
    render(<MissingFiles />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Remap (1)" }));
    fireEvent.click(screen.getByRole("button", { name: /Search the library/ }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "kiss" } });
    fireEvent.click(await screen.findByRole("button", { name: /found\\kiss\.flac/ }));

    await waitFor(() => expect(callsWith("POST")).toHaveLength(1));
    const [url, init] = callsWith("POST")[0];
    expect(url).toBe(`/api/v1/library/missing-files/${A}/remap`);
    expect(JSON.parse(init.body as string)).toEqual({ target_file_id: FOUND });
  });

  it("shows why a remap was refused", async () => {
    mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "POST")
        return Promise.reject(new Error(`File ${COPY} is missing from disk`));
      if (url.startsWith("/api/v1/library/missing-files?")) return Promise.resolve(PAGE);
      return Promise.resolve({});
    });
    render(<MissingFiles />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Remap (1)" }));
    fireEvent.click(screen.getByRole("button", { name: "Use this file" }));

    expect((await screen.findByRole("alert")).textContent).toContain("missing from disk");
  });
});
