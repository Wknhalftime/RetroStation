// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ConflictError, ServerError, apiFetch } from "@/api/client";
import { LibraryStatus } from "./LibraryStatus";

// Artist model phase 3b PR 4, SPEC D6 and D28: Library Status links to the merge list with the
// number of planned groups, like Credit picks. The link shows while any of the page's three
// sections has rows (groups, recordings under two works, empty works kept), so the empty works
// stay reachable after the last merge; with none it shows no link. A count that fails, that the
// server refuses (409 until D1's credits), or that answers something else (the locked
// LibraryStatus tests answer unknown URLs with undefined) hides the link and shows no error.

const mockedApiFetch = vi.mocked(apiFetch);
const STATUS = { total_files: 10, quarantine_count: 0, by_format: {}, by_enrichment: {} };
const COUNT = "/api/v1/library/merge-list/count";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function serve(count: () => Promise<unknown>) {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === "/api/v1/library/status") return Promise.resolve(STATUS);
    if (url.startsWith("/api/v1/library/missing-files?"))
      return Promise.resolve({ items: [], total: 0, total_match_count: 0 });
    if (url.startsWith("/api/v1/library/versions?"))
      return Promise.resolve({ items: [], total: 0, new_total: 0 });
    if (url === COUNT) return count();
    return Promise.resolve(undefined);
  });
}

function countRequested(): boolean {
  return mockedApiFetch.mock.calls.some(([url]) => url === COUNT);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("LibraryStatus: the merge list link", () => {
  it("links to the page with the number of planned groups", async () => {
    serve(() => Promise.resolve({ total: 117, duplicates: 49, kept_works: 3 }));
    render(<LibraryStatus />, { wrapper });

    const link = await screen.findByRole("link", { name: /Merge list/ });
    expect(link.getAttribute("href")).toBe("/library/merge-list");
    expect(link.textContent).toContain("117");
  });

  it.each([
    ["recordings under two works", { total: 0, duplicates: 2, kept_works: 0 }],
    ["empty works kept", { total: 0, duplicates: 0, kept_works: 5 }],
  ])("still links to the page when only %s are left", async (_, count) => {
    serve(() => Promise.resolve(count));
    render(<LibraryStatus />, { wrapper });

    const link = await screen.findByRole("link", { name: /Merge list/ });
    expect(link.getAttribute("href")).toBe("/library/merge-list");
    expect(link.textContent).toContain("0");
  });

  it("shows no link when every section is empty", async () => {
    serve(() => Promise.resolve({ total: 0, duplicates: 0, kept_works: 0 }));
    render(<LibraryStatus />, { wrapper });

    await screen.findByText("Total Files");
    await waitFor(() => expect(countRequested()).toBe(true));
    expect(screen.queryByRole("link", { name: /Merge list/ })).toBeNull();
  });

  it.each([
    ["fails", () => Promise.reject(new ServerError(500))],
    ["is refused until the credits are done", () => Promise.reject(new ConflictError("Credits"))],
    ["answers something else", () => Promise.resolve(undefined)],
  ])("hides the link and shows no error when the count %s", async (_, count) => {
    serve(count);
    render(<LibraryStatus />, { wrapper });

    await screen.findByText("Total Files");
    await waitFor(() => expect(countRequested()).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole("link", { name: /Merge list/ })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Failed/)).toBeNull();
  });
});
