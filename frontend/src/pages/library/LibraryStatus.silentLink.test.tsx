// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
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

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === "/api/v1/library/status") return Promise.resolve(STATUS);
    if (url.startsWith("/api/v1/library/missing-files?"))
      return Promise.resolve({ items: [], total: 0, total_match_count: 0 });
    if (url === "/api/v1/settings") return Promise.resolve({});
    return Promise.resolve(undefined);
  });
});

describe("silent files link", () => {
  it("links the Library page to the Silent Files report", async () => {
    render(<LibraryStatus />, { wrapper });
    const link = await screen.findByRole("link", { name: /Silent Files/ });
    expect(link.getAttribute("href")).toBe("/library/silent");
  });
});
