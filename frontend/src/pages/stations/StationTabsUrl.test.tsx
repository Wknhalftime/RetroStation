// @vitest-environment jsdom
// PR L3, Task 16 fix round 1: the station dashboard's tab lives in the URL (?tab=), so Back from
// a liner's cues returns to the Liners tab. Choosing a tab replaces the history entry (Back from
// the editor goes straight to the liners, not through every tab clicked); the liner editor's
// fallback with no history opens /stations/<id>?tab=liners.
import { describe, it, expect, vi, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { StationDashboard } from "./StationDashboard";
import { LinerCueEditor } from "./LinerCueEditor";

const STATION = "11111111-1111-4111-8111-111111111111";
const LINER = "l1";
const DASHBOARD = `/stations/${STATION}`;
const EDITOR = `/stations/${STATION}/liners/${LINER}/cues`;
const NO_SUCH_LINER = "This station has no such liner.";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
      const path = new URL(String(input)).pathname;
      if (path === `/api/v1/stations/${STATION}`) {
        return json(200, {
          id: STATION,
          call_letters: "KQRS",
          name: "Rock 92",
          city: "Minneapolis",
          format_name: "Album Rock",
          created_at: "2026-01-01T00:00:00Z",
        });
      }
      if (path === "/api/v1/playlists") return json(200, []);
      if (path === `/api/v1/stations/${STATION}/liners`) {
        return json(200, {
          songs_between_liners: 4,
          liners: [
            {
              id: LINER,
              name: "Station ID",
              format: "mp3",
              duration_ms: 8000,
              cues: "auto",
              uploaded_at: "2026-10-17T10:00:00Z",
            },
          ],
        });
      }
      // The liner editor cannot open this liner: enough to reach its back button.
      if (path === `/api/v1/stations/${STATION}/liners/${LINER}/cues`) {
        return json(404, { detail: NO_SUCH_LINER });
      }
      return json(404, { detail: `unexpected ${path}` });
    })
  );
}

/** Where the router is, and a Back that steps one entry back in its history. */
function Probe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output aria-label="location">{`${location.pathname}${location.search}`}</output>
      <button type="button" onClick={() => navigate(-1)}>
        History back
      </button>
    </>
  );
}

function renderAt(entries: string[], initialIndex?: number) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={entries} initialIndex={initialIndex}>
        <Routes>
          <Route path="/stations/:station_id" element={<StationDashboard />} />
          <Route path="/stations/:station_id/liners/:liner_id/cues" element={<LinerCueEditor />} />
          <Route path="/previous" element={<p>The page before</p>} />
        </Routes>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function location(): string {
  return screen.getByLabelText("location").textContent ?? "";
}

async function linersTabShowing() {
  await screen.findByLabelText("Songs between liners");
  expect(screen.queryByText("Upload Playlist CSV")).toBeNull();
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("StationDashboard: the tab in the URL", () => {
  it("?tab=liners opens on the Liners tab", async () => {
    serve();
    renderAt([`${DASHBOARD}?tab=liners`]);

    await linersTabShowing();
  });

  it.each(["", "?tab=nonsense"])("%s opens on Overview", async (search) => {
    serve();
    renderAt([`${DASHBOARD}${search}`]);

    expect(await screen.findByText("Upload Playlist CSV")).toBeTruthy();
    expect(screen.queryByLabelText("Songs between liners")).toBeNull();
  });

  it("choosing a tab sets ?tab, replacing the history entry", async () => {
    serve();
    renderAt(["/previous", DASHBOARD], 1);
    await screen.findByText("Upload Playlist CSV");

    fireEvent.click(screen.getByRole("button", { name: "Liners" }));
    await waitFor(() => expect(location()).toBe(`${DASHBOARD}?tab=liners`));
    await linersTabShowing();

    fireEvent.click(screen.getByRole("button", { name: "Missing Matches" }));
    await waitFor(() => expect(location()).toBe(`${DASHBOARD}?tab=missing-matches`));

    fireEvent.click(screen.getByRole("button", { name: "Overview" }));
    await waitFor(() => expect(location()).toBe(`${DASHBOARD}?tab=overview`));
    expect(await screen.findByText("Upload Playlist CSV")).toBeTruthy();

    // No entry was added per tab: one step back leaves the dashboard.
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    expect(await screen.findByText("The page before")).toBeTruthy();
  });
});

describe("LinerCueEditor: back to the Liners tab", () => {
  it("Liners tab → Edit cues → Back to the liners returns to the Liners tab", async () => {
    serve();
    renderAt([DASHBOARD]);
    await screen.findByText("Upload Playlist CSV");
    fireEvent.click(screen.getByRole("button", { name: "Liners" }));
    await linersTabShowing();

    fireEvent.click(await screen.findByRole("link", { name: "Edit cues" }));
    await screen.findByText(NO_SUCH_LINER);
    expect(location()).toBe(EDITOR);

    fireEvent.click(screen.getByRole("button", { name: "Back to the liners" }));

    await waitFor(() => expect(location()).toBe(`${DASHBOARD}?tab=liners`));
    await linersTabShowing();
  });

  it("with no history, Back to the liners opens the station's Liners tab", async () => {
    serve();
    renderAt([EDITOR]);
    await screen.findByText(NO_SUCH_LINER);

    fireEvent.click(screen.getByRole("button", { name: "Back to the liners" }));

    await waitFor(() => expect(location()).toBe(`${DASHBOARD}?tab=liners`));
    await linersTabShowing();
  });
});
