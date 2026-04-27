// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "@/api/client";
import { SearchSlideOver } from "./SearchSlideOver";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const mockedApiFetch = vi.mocked(apiFetch);

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("SearchSlideOver mb-artist mode", () => {
  it("stops fetching after the slide-over closes (open gate guards refetches)", async () => {
    // The `useMbArtistSearch` hook is `enabled: trimmed.length > 0`, so an
    // empty-query render alone doesn't exercise the `open` gate (an
    // empty-query test would pass even if the gate regressed). A meaningful
    // test must:
    //   1. Render open, type a real query, wait for the fetch to fire.
    //   2. Rerender with open=false and invalidate the query cache. If the
    //      `open` gate regressed, React Query would re-fire queryFn because
    //      the hook would still be enabled; with the gate, the hook is
    //      disabled and invalidation is a no-op.
    mockedApiFetch.mockResolvedValue({ items: [] });
    const qc = makeClient();
    const { rerender } = render(
      <SearchSlideOver open={true} onClose={vi.fn()} mode="mb-artist" />,
      { wrapper: wrapperFor(qc) }
    );
    const input = screen.getByRole("searchbox");
    fireEvent.change(input, { target: { value: "prince" } });
    // Wait for the debounce to fire and React Query to dispatch the fetch.
    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalled();
    });
    const fetchCountAfterOpen = mockedApiFetch.mock.calls.length;

    // Close the slide-over, then force any eligible refetch via invalidate.
    // With the gate in place, the hook is disabled so invalidation cannot
    // trigger queryFn. Without the gate, a fetch would fire here.
    rerender(<SearchSlideOver open={false} onClose={vi.fn()} mode="mb-artist" />);
    await qc.invalidateQueries({ queryKey: ["mb-artist-search"] });
    // Yield microtasks so any scheduled queryFn would have a chance to run.
    await Promise.resolve();
    expect(mockedApiFetch.mock.calls.length).toBe(fetchCountAfterOpen);
  });

  it("routes search through the mb-artists endpoint when open", async () => {
    mockedApiFetch.mockResolvedValue({
      items: [
        {
          id: "mbid-prince-xxx",
          name: "Prince",
          score: 100,
          disambiguation: "",
        },
      ],
    });
    render(<SearchSlideOver open={true} onClose={vi.fn()} mode="mb-artist" />, {
      wrapper: wrapperFor(makeClient()),
    });
    const input = screen.getByRole("searchbox");
    fireEvent.change(input, { target: { value: "prince" } });
    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/matching/mb-artists?query=prince")
      );
    });
    // The library-artists endpoint must NOT be hit in mb-artist mode.
    expect(mockedApiFetch).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/library/artists")
    );
  });

  it("invokes onSelectMbArtist with the MBID on result click", async () => {
    mockedApiFetch.mockResolvedValue({
      items: [
        {
          id: "mbid-prince-xxx",
          name: "Prince",
          score: 100,
          disambiguation: "The Artist",
        },
      ],
    });
    const onSelectMbArtist = vi.fn();
    const onClose = vi.fn();
    render(
      <SearchSlideOver
        open={true}
        onClose={onClose}
        mode="mb-artist"
        onSelectMbArtist={onSelectMbArtist}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const input = screen.getByRole("searchbox");
    fireEvent.change(input, { target: { value: "prince" } });
    const resultButton = await screen.findByRole("button", {
      name: /Prince/i,
    });
    fireEvent.click(resultButton);
    expect(onSelectMbArtist).toHaveBeenCalledWith(
      expect.objectContaining({ id: "mbid-prince-xxx", name: "Prince" })
    );
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('gates the "No results found" empty-state on trimmed query', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] });
    // Fake timers so we can deterministically advance past the 300ms
    // input debounce and assert no empty state (and no fetch) ever
    // fires for whitespace-only input.
    vi.useFakeTimers();
    try {
      render(<SearchSlideOver open={true} onClose={vi.fn()} mode="mb-artist" />, {
        wrapper: wrapperFor(makeClient()),
      });
      const input = screen.getByRole("searchbox");
      fireEvent.change(input, { target: { value: "   " } });
      await vi.advanceTimersByTimeAsync(500);
      expect(screen.queryByText("No results found.")).toBeNull();
      expect(mockedApiFetch).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("closes on Escape key", () => {
    const onClose = vi.fn();
    render(<SearchSlideOver open={true} onClose={onClose} mode="mb-artist" />, {
      wrapper: wrapperFor(makeClient()),
    });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("SearchSlideOver file mode", () => {
  it("routes search through /api/v1/library/files when mode is 'file'", async () => {
    mockedApiFetch.mockResolvedValue({ items: [] });
    render(
      <SearchSlideOver open={true} onClose={vi.fn()} mode="file" />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const input = screen.getByRole("searchbox");
    fireEvent.change(input, { target: { value: "hurts" } });
    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/library/files?")
      );
    });
    // Must NOT hit the artists endpoint.
    expect(mockedApiFetch).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/library/artists")
    );
  });

  it("renders track_title and file_path on each result", async () => {
    mockedApiFetch.mockResolvedValue({
      items: [
        { id: "file-uuid-1", file_path: "/music/x.flac", track_title: "Everybody Hurts" },
      ],
    });
    render(
      <SearchSlideOver open={true} onClose={vi.fn()} mode="file" />,
      { wrapper: wrapperFor(makeClient()) }
    );
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "everybody" } });
    await screen.findByText("Everybody Hurts");
    expect(screen.getByText("/music/x.flac")).toBeTruthy();
  });

  it("falls back to 'Untitled' when track_title is null", async () => {
    mockedApiFetch.mockResolvedValue({
      items: [{ id: "file-uuid-2", file_path: "/music/notitle.flac", track_title: null }],
    });
    render(
      <SearchSlideOver open={true} onClose={vi.fn()} mode="file" />,
      { wrapper: wrapperFor(makeClient()) }
    );
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "notitle" } });
    await screen.findByText("Untitled");
    expect(screen.getByText("/music/notitle.flac")).toBeTruthy();
  });

  it("passes artist_mbid when 'Restrict to confirmed artist' is checked", async () => {
    mockedApiFetch.mockResolvedValue({ items: [] });
    render(
      <SearchSlideOver
        open={true}
        onClose={vi.fn()}
        mode="file"
        restrictArtistMbid="mbid-rem-xxx"
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "everybody" } });
    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalledWith(
        expect.stringContaining("artist_mbid=mbid-rem-xxx")
      );
    });
  });
});
