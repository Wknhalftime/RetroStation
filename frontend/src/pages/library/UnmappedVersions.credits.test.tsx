// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { apiFetch } from "@/api/client";
import { UnmappedVersions } from "./UnmappedVersions";

// Phase 3b PR 5b (SPEC S7; scope note R7; D41 b, D50; AUD-R082, AUD-R088): a work has no artist
// of its own, so an Unmapped versions item names its work's credited artists (`artists`, no
// `artist_id` or `artist_name`). The Artist column joins their names, or reads "No credited
// artist" when there are none (D50's words); Open work goes to the work's own address.

const mockedApiFetch = vi.mocked(apiFetch);

function item(work: string, title: string, artists: { id: string | null; name: string }[]) {
  return {
    work_id: work,
    work_title: title,
    artists,
    version_text: "live",
    version_type: "live",
    reads_as_version: true,
    plays: 10,
    stations: ["KISW"],
    first_seen: "2001-05-03",
    first_station: "KISW",
    new: false,
    suggestion: null,
  };
}

const DUET = item("w-duet", "Who Says You Can't Go Home", [
  { id: "a-bj", name: "Bon Jovi" },
  { id: "a-jn", name: "Jennifer Nettles" },
]);
const COMPILATION = item("w-va", "Lady Marmalade", []);

function renderPage() {
  mockedApiFetch.mockImplementation((url: string) =>
    url.startsWith("/api/v1/library/versions?")
      ? Promise.resolve({ items: [DUET, COMPILATION], total: 2, new_total: 0 })
      : Promise.reject(new Error(`unexpected ${url}`))
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <UnmappedVersions />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("UnmappedVersions, a work's artists from its credits", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("names the work's credited artists in the Artist column", async () => {
    renderPage();

    const row = await screen.findByRole("row", { name: "Who Says You Can't Go Home (live)" });
    expect(within(row).getByText("Bon Jovi, Jennifer Nettles")).toBeTruthy();
  });

  it("reads 'No credited artist' for a work whose performances credit no one", async () => {
    renderPage();

    const row = await screen.findByRole("row", { name: "Lady Marmalade (live)" });
    expect(within(row).getByText("No credited artist")).toBeTruthy();
  });

  it("opens the work at its own address", async () => {
    renderPage();

    const row = await screen.findByRole("row", { name: "Lady Marmalade (live)" });
    const open = within(row).getByRole("link", { name: "Open Lady Marmalade" });
    expect(open.getAttribute("href")).toBe("/library/works/w-va");
  });
});
