// @vitest-environment jsdom
// PR L1, Task 6 (traceability LR5): the station dashboard gets a third tab, "Liners", after
// Overview and Missing Matches. Choosing it shows the Liners tab for this station.
import { describe, it, expect, vi, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { StationDashboard } from "./StationDashboard";

const STATION = "11111111-1111-4111-8111-111111111111";

type Recorded = { method: string; path: string };

function serve(): Recorded[] {
  const calls: Recorded[] = [];
  const json = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ method, path: url.pathname });
      if (url.pathname === `/api/v1/stations/${STATION}`) {
        return json(200, {
          id: STATION,
          call_letters: "KQRS",
          name: "Rock 92",
          city: "Minneapolis",
          format_name: "Album Rock",
          created_at: "2026-01-01T00:00:00Z",
        });
      }
      if (url.pathname === "/api/v1/playlists") return json(200, []);
      if (url.pathname === `/api/v1/stations/${STATION}/liners`) {
        return json(200, {
          songs_between_liners: 4,
          liners: [
            {
              id: "l1",
              name: "Station ID",
              format: "mp3",
              duration_ms: 8000,
              cues: "auto",
              uploaded_at: "2026-10-17T10:00:00Z",
            },
          ],
        });
      }
      return json(404, { detail: `unexpected ${method} ${url.pathname}` });
    })
  );
  return calls;
}

function renderDashboard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/stations/${STATION}`]}>
        <Routes>
          <Route path="/stations/:station_id" element={<StationDashboard />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("StationDashboard", () => {
  it("the dashboard has a Liners tab that shows the tab", async () => {
    const calls = serve();
    renderDashboard();

    const overview = await screen.findByRole("button", { name: "Overview" });
    const missing = screen.getByRole("button", { name: "Missing Matches" });
    const tab = screen.getByRole("button", { name: "Liners" });

    // It is the third tab, after Overview and Missing Matches.
    const follows = (a: HTMLElement, b: HTMLElement) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(overview, missing)).toBe(true);
    expect(follows(missing, tab)).toBe(true);

    // The Overview panel is showing until the tab is chosen.
    expect(screen.getByText("Upload Playlist CSV")).toBeTruthy();
    expect(screen.queryByLabelText("Songs between liners")).toBeNull();

    fireEvent.click(tab);

    // The Liners tab shows for this station: its cadence box and its liners.
    const box = await screen.findByLabelText("Songs between liners");
    await waitFor(() => expect((box as HTMLInputElement).value).toBe("4"));
    expect(await screen.findByText("Station ID")).toBeTruthy();
    expect(
      calls.some((c) => c.method === "GET" && c.path === `/api/v1/stations/${STATION}/liners`)
    ).toBe(true);
    // ...in place of the Overview panel, as the other tabs do.
    expect(screen.queryByText("Upload Playlist CSV")).toBeNull();

    // Going back to Overview hides it again.
    fireEvent.click(overview);
    expect(await screen.findByText("Upload Playlist CSV")).toBeTruthy();
    expect(screen.queryByLabelText("Songs between liners")).toBeNull();
  });
});
