// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";
import { LibraryStatus } from "./LibraryStatus";

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

function serve(missingTotal: number) {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === "/api/v1/library/status") return Promise.resolve(STATUS);
    if (url.startsWith("/api/v1/library/missing-files?"))
      return Promise.resolve({ items: [], total: missingTotal, total_match_count: 0 });
    if (url === "/api/v1/settings") return Promise.resolve({});
    return Promise.resolve(undefined);
  });
}

beforeEach(() => { mockedApiFetch.mockReset(); });

describe("LibraryStatus", () => {
  it("links to the Missing Files page with the count", async () => {
    serve(383);
    render(<LibraryStatus />, { wrapper });

    const link = await screen.findByRole("link", { name: /Missing files/ });
    expect(link.getAttribute("href")).toBe("/library/missing");
    expect(link.textContent).toContain("383");
  });

  it("shows no link when nothing is missing", async () => {
    serve(0);
    render(<LibraryStatus />, { wrapper });

    await screen.findByText("Total Files");
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/v1\/library\/missing-files\?/)
      )
    );
    expect(screen.queryByRole("link", { name: /Missing files/ })).toBeNull();
  });
});
