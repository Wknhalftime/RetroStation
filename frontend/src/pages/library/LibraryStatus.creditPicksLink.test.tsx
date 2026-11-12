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

import { ServerError, apiFetch } from "@/api/client";
import { LibraryStatus } from "./LibraryStatus";

// Performances phase 3a PR 3, SPEC S9 and D7: Library Status links to the Credit picks page with
// the count of proposals waiting for Lance, like Missing files; with none it shows no link. A
// count that fails to load, or that answers something else (the locked LibraryStatus tests answer
// unknown URLs with undefined), hides the link and shows no error.

const mockedApiFetch = vi.mocked(apiFetch);
const STATUS = { total_files: 10, quarantine_count: 0, by_format: {}, by_enrichment: {} };
const COUNT = "/api/v1/library/credit-picks/count";

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

describe("LibraryStatus: the Credit picks link", () => {
  it("links to the page with the count", async () => {
    serve(() => Promise.resolve({ total: 37 }));
    render(<LibraryStatus />, { wrapper });

    const link = await screen.findByRole("link", { name: /Credit picks/ });
    expect(link.getAttribute("href")).toBe("/library/credit-picks");
    expect(link.textContent).toContain("37");
  });

  it("shows no link when nothing waits", async () => {
    serve(() => Promise.resolve({ total: 0 }));
    render(<LibraryStatus />, { wrapper });

    await screen.findByText("Total Files");
    await waitFor(() => expect(countRequested()).toBe(true));
    expect(screen.queryByRole("link", { name: /Credit picks/ })).toBeNull();
  });

  it.each([
    ["fails", () => Promise.reject(new ServerError(500))],
    ["answers something else", () => Promise.resolve(undefined)],
  ])("hides the link and shows no error when the count %s", async (_, count) => {
    serve(count);
    render(<LibraryStatus />, { wrapper });

    await screen.findByText("Total Files");
    await waitFor(() => expect(countRequested()).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole("link", { name: /Credit picks/ })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Failed/)).toBeNull();
  });
});
