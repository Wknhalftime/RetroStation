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

// Versions phase 2b PR 3, SPEC S7 and E6: Library Status links to the Unmapped versions page
// with its count, and how many are new; with none it shows no link. A count that fails to load,
// or that answers something else (the locked LibraryStatus tests answer unknown URLs with
// undefined), hides the link and shows no error.

const mockedApiFetch = vi.mocked(apiFetch);
const STATUS = { total_files: 10, quarantine_count: 0, by_format: {}, by_enrichment: {} };

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function serve(versions: () => Promise<unknown>) {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === "/api/v1/library/status") return Promise.resolve(STATUS);
    if (url.startsWith("/api/v1/library/missing-files?"))
      return Promise.resolve({ items: [], total: 0, total_match_count: 0 });
    if (url.startsWith("/api/v1/library/versions?")) return versions();
    return Promise.resolve(undefined);
  });
}

function versionsRequested(): boolean {
  return mockedApiFetch.mock.calls.some(([url]) => url.startsWith("/api/v1/library/versions?"));
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("LibraryStatus: the Unmapped versions link", () => {
  it("links to the page with the count and how many are new", async () => {
    serve(() => Promise.resolve({ items: [], total: 799, new_total: 3 }));
    render(<LibraryStatus />, { wrapper });

    const link = await screen.findByRole("link", { name: /Unmapped versions/ });
    expect(link.getAttribute("href")).toBe("/library/versions");
    expect(link.textContent).toContain("799");
    expect(link.textContent).toContain("3 new");
  });

  it("says nothing of new ones when there are none", async () => {
    serve(() => Promise.resolve({ items: [], total: 12, new_total: 0 }));
    render(<LibraryStatus />, { wrapper });

    const link = await screen.findByRole("link", { name: /Unmapped versions/ });
    expect(link.textContent).toContain("12");
    expect(link.textContent).not.toMatch(/new/);
  });

  it("shows no link when nothing is unmapped", async () => {
    serve(() => Promise.resolve({ items: [], total: 0, new_total: 0 }));
    render(<LibraryStatus />, { wrapper });

    await screen.findByText("Total Files");
    await waitFor(() => expect(versionsRequested()).toBe(true));
    expect(screen.queryByRole("link", { name: /Unmapped versions/ })).toBeNull();
  });

  it.each([
    ["fails", () => Promise.reject(new ServerError(500))],
    ["answers something else", () => Promise.resolve(undefined)],
  ])("hides the link and shows no error when the count %s (E6)", async (_, versions) => {
    serve(versions);
    render(<LibraryStatus />, { wrapper });

    await screen.findByText("Total Files");
    await waitFor(() => expect(versionsRequested()).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole("link", { name: /Unmapped versions/ })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Failed/)).toBeNull();
  });
});
