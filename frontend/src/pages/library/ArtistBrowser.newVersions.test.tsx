// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

// jsdom lays nothing out, so the list's virtualiser would show no rows: show every row.
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({
        index,
        key: index,
        start: index * 56,
        size: 56,
      })),
    getTotalSize: () => count * 56,
    measureElement: () => undefined,
  }),
}));

import { apiFetch } from "@/api/client";
import { ArtistBrowser } from "./ArtistBrowser";

// Versions phase 2b PR 3, SPEC S7 and D5 (C62): the artist list shows "N new versions" on an
// artist whose group has new ones (counted on the group's primary by the server); the field is
// optional, so an artist without it, or with 0, shows nothing.

const mockedApiFetch = vi.mocked(apiFetch);

function summary(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    name,
    sort_name: name,
    disambiguation: null,
    work_count: 3,
    file_count: 5,
    mbid: null,
    origin: "local",
    ...extra,
  };
}

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
    if (url.startsWith("/api/v1/library/artists?"))
      return Promise.resolve({
        items: [
          summary("a-prince", "Prince", { new_version_count: 2 }),
          summary("a-korn", "Korn", { new_version_count: 1 }),
          summary("a-quiet", "Quiet", { new_version_count: 0 }),
          summary("a-old", "Older Server"),
        ],
        total: 4,
      });
    return Promise.resolve(undefined);
  });
});

describe("ArtistBrowser: new versions", () => {
  it("shows how many new versions each artist has, and nothing for none", async () => {
    render(<ArtistBrowser />, { wrapper });

    const prince = await screen.findByRole("link", { name: /Prince/ });
    expect(prince.textContent).toContain("2 new versions");
    expect(screen.getByRole("link", { name: /Korn/ }).textContent).toContain("1 new version");
    expect(screen.getByRole("link", { name: /Korn/ }).textContent).not.toContain("versions");
    expect(screen.getByRole("link", { name: /Quiet/ }).textContent).not.toMatch(/new version/);
    expect(screen.getByRole("link", { name: /Older Server/ }).textContent).not.toMatch(
      /new version/
    );
  });
});
