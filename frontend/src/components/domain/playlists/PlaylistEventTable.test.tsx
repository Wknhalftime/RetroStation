// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PaginatedEvents } from "@/lib/schemas/playlists";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
  apiDownload: vi.fn(),
}));

import { apiFetch } from "@/api/client";
import { PlaylistEventTable } from "./PlaylistEventTable";

const events: PaginatedEvents = {
  items: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      // Logged at 23:30 station time; the API serializes it in America/Chicago.
      played_at: "2001-03-15T18:30:00-05:00",
      artist_name: "Blondie",
      title: "Call Me",
      match_status: "matched",
      match_tier: null,
    },
  ],
  total: 1,
};

// A browser west of UTC is where the local-time conversion shows up.
beforeEach(() => {
  vi.stubEnv("TZ", "America/Chicago");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("PlaylistEventTable", () => {
  it("shows played_at as the station's wall-clock time", async () => {
    vi.mocked(apiFetch).mockResolvedValue(events);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={qc}>
        <PlaylistEventTable playlistId="playlist-1" />
      </QueryClientProvider>
    );

    expect(await screen.findByText("Mar 15, 2001, 11:30 PM")).toBeTruthy();
  });
});
