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

import { apiFetch } from "@/api/client";
import { LibraryStatus } from "./LibraryStatus";

// Artist model phase 3b PR 5c, SPEC D58 (D28's reason, extended): the merge list link also shows
// while the page's "Kept apart" section has rows, so its Undo stays reachable after the last
// merge. The number stays the planned-group count. A count with no kept_apart (the locked
// fixtures) counts it as 0.

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

function serve(count: unknown) {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === "/api/v1/library/status") return Promise.resolve(STATUS);
    if (url.startsWith("/api/v1/library/missing-files?"))
      return Promise.resolve({ items: [], total: 0, total_match_count: 0 });
    if (url.startsWith("/api/v1/library/versions?"))
      return Promise.resolve({ items: [], total: 0, new_total: 0 });
    if (url === COUNT) return Promise.resolve(count);
    return Promise.resolve(undefined);
  });
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("LibraryStatus: the merge list link and kept pairs (D58)", () => {
  it("still links to the page when only kept-apart pairs are left", async () => {
    serve({ total: 0, duplicates: 0, kept_works: 0, kept_apart: 2 });
    render(<LibraryStatus />, { wrapper });

    const link = await screen.findByRole("link", { name: /Merge list/ });
    expect(link.getAttribute("href")).toBe("/library/merge-list");
    expect(link.textContent).toContain("0");
    expect(link.textContent).not.toContain("2");
  });

  it("numbers the planned groups, not the kept pairs", async () => {
    serve({ total: 7, duplicates: 0, kept_works: 0, kept_apart: 3 });
    render(<LibraryStatus />, { wrapper });

    const link = await screen.findByRole("link", { name: /Merge list/ });
    expect(link.textContent).toContain("7");
    expect(link.textContent).not.toContain("3");
  });

  it("shows no link when every section, kept pairs included, is empty", async () => {
    serve({ total: 0, duplicates: 0, kept_works: 0, kept_apart: 0 });
    render(<LibraryStatus />, { wrapper });

    await screen.findByText("Total Files");
    await waitFor(() =>
      expect(mockedApiFetch.mock.calls.some(([url]) => url === COUNT)).toBe(true)
    );
    expect(screen.queryByRole("link", { name: /Merge list/ })).toBeNull();
  });
});
