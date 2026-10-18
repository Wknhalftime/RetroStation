// @vitest-environment jsdom
// Station liners, PR L3 Task 16 (traceability LR11; LR4 for the silent liner; LR5 for the
// Liners tab's Cues column): the cue editor opened on a liner, at
// /stations/:station_id/liners/:liner_id/cues. It reuses the song editor's body with the Fade-out
// marker hidden and pinned to Stop, and talks to the Task 15 routes:
//   GET/PUT/DELETE /api/v1/stations/{station_id}/liners/{liner_id}/cues
//   GET            /api/v1/stations/{station_id}/liners/{liner_id}/waveform
//   GET            /api/v1/stations/{station_id}/liners/{liner_id}/audio   (L1's route)
// As in the song editor's tests (pages/library/CueEditor.test.tsx), `fetch` is stubbed with a
// small fake of that API, so the real client, schemas and hooks run. Canvas drawing and the
// Web Audio preview are not exercised here (jsdom has neither); the song editor's tests cover them.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { LinerCueEditor } from "./LinerCueEditor";

const STATION = "11111111-1111-4111-8111-111111111111";
const LINER = "33333333-3333-4333-8333-333333333333";
const API = `/api/v1/stations/${STATION}/liners/${LINER}`;
const CUES_PATH = `${API}/cues`;
const WAVEFORM_PATH = `${API}/waveform`;
const AUDIO_PATH = `${API}/audio`;
const PAGE_URL = `/stations/${STATION}/liners/${LINER}/cues`;

interface ApiMarkers {
  start_ms: number;
  eom_ms: number;
  fade_at_ms: number;
  stop_ms: number;
}

// A 7 s liner as autocue measured it: Start 0:00.120, EOM 0:06.600, Stop 0:06.900; the API
// always sends a liner's Fade-out at its Stop.
const VIEW_DURATION_MS = 7_000;
const AUTOMATIC: ApiMarkers = { start_ms: 120, eom_ms: 6_600, fade_at_ms: 6_900, stop_ms: 6_900 };
// A saved edit: Start 0:00.500, EOM 0:06.000, Stop 0:06.800.
const MANUAL: ApiMarkers = { start_ms: 500, eom_ms: 6_000, fade_at_ms: 6_800, stop_ms: 6_800 };
const PEAKS_B64 = btoa(String.fromCharCode(...new Array<number>(70).fill(128)));

const AUTOMATIC_TIMES = { start: "0:00.120", eom: "0:06.600", stop: "0:06.900" };
const MANUAL_TIMES = { start: "0:00.500", eom: "0:06.000", stop: "0:06.800" };

const LINER_SILENT_NOTE =
  "Measured silent — the radio skips this liner, so its cues can't be edited.";
const SONG_SILENT_NOTE =
  "Measured silent — the radio skips this song, so its cues can't be edited.";
const NO_SUCH_LINER = "This station has no such liner.";

// ---------------------------------------------------------------------------
// A fake of the Task 15 API (and L1's audio route)
// ---------------------------------------------------------------------------

interface Call {
  method: string;
  path: string;
  body: unknown;
  token: string | null;
}

interface Options {
  analysis?: "measured" | "failed" | "waiting" | "silent";
  manual?: ApiMarkers | null;
  editorError?: { status: number; detail: string };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function serve(options: Options = {}) {
  const calls: Call[] = [];
  let manual: ApiMarkers | null = options.manual ?? null;

  vi.spyOn(globalThis, "fetch").mockImplementation(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const href =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(href).pathname;
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
      calls.push({ method, path, body, token: new Headers(init?.headers).get("X-Airwave-Token") });

      if (path === CUES_PATH && method === "GET") {
        if (options.editorError) {
          return json(options.editorError.status, { detail: options.editorError.detail });
        }
        return json(200, {
          file_id: LINER,
          title: "Station ID",
          artist: null,
          format: "mp3",
          duration_ms: VIEW_DURATION_MS,
          analysis: options.analysis ?? "measured",
          automatic: AUTOMATIC,
          manual,
        });
      }
      if (path === CUES_PATH && method === "PUT") {
        const sent = body as { start_ms: number; eom_ms: number; stop_ms: number };
        manual = { ...sent, fade_at_ms: sent.stop_ms };
        return new Response(null, { status: 204 });
      }
      if (path === CUES_PATH && method === "DELETE") {
        manual = null;
        return new Response(null, { status: 204 });
      }
      if (path === WAVEFORM_PATH && method === "GET") {
        return json(200, { duration_ms: VIEW_DURATION_MS, per_second: 10, peaks: PEAKS_B64 });
      }
      if (path === AUDIO_PATH && method === "GET") {
        return new Response(new Uint8Array([1, 2, 3, 4]), { status: 200 });
      }
      return json(404, { detail: `unexpected ${method} ${path}` });
    }
  );

  return {
    calls,
    puts: () => calls.filter((c) => c.method === "PUT"),
    deletes: () => calls.filter((c) => c.method === "DELETE"),
    editorReads: () => calls.filter((c) => c.method === "GET" && c.path === CUES_PATH),
  };
}

// ---------------------------------------------------------------------------
// Rendering and reading the page
// ---------------------------------------------------------------------------

function newClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderPage(
  entries: string[] = [PAGE_URL],
  initialIndex?: number,
  qc: QueryClient = newClient()
) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={entries} initialIndex={initialIndex}>
        {children}
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(
    <Routes>
      <Route path="/stations/:station_id/liners/:liner_id/cues" element={<LinerCueEditor />} />
      <Route path="/stations/:station_id" element={<p>The station page</p>} />
      <Route path="/previous" element={<p>The page before</p>} />
    </Routes>,
    { wrapper }
  );
}

async function openEditor(qc?: QueryClient) {
  const view = renderPage([PAGE_URL], undefined, qc);
  await screen.findByRole("table");
  return view;
}

type Name = "Start" | "EOM" | "Fade-out" | "Stop";

function leadingName(row: HTMLElement): string {
  return row.children[0]?.textContent?.trim() ?? "";
}

/** The marker names the table lists, in order (the header row has none of them). */
function markerNames(): string[] {
  const names: Name[] = ["Start", "EOM", "Fade-out", "Stop"];
  return within(screen.getByRole("table"))
    .getAllByRole("row")
    .map((row) => leadingName(row))
    .map((cell) => names.find((name) => new RegExp(`^[^A-Za-z]*${name}(?![A-Za-z-])`).test(cell)))
    .filter((name): name is Name => name !== undefined);
}

function markerRow(name: Name): HTMLElement {
  const leading = new RegExp(`^[^A-Za-z]*${name}(?![A-Za-z-])`);
  const row = screen.getAllByRole("row").find((candidate) => leading.test(leadingName(candidate)));
  if (!row) throw new Error(`no table row for ${name}`);
  return row;
}

function cellText(row: HTMLElement, index: number): string {
  return row.children[index]?.textContent?.trim() ?? "";
}

/** The Time column (second cell) of each marker a liner shows. */
function times() {
  return {
    start: cellText(markerRow("Start"), 1),
    eom: cellText(markerRow("EOM"), 1),
    stop: cellText(markerRow("Stop"), 1),
  };
}

function nudgeButton(marker: Name, label: string) {
  return within(markerRow(marker)).getByRole("button", { name: label });
}

function button(name: string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

/** No Fade-out anywhere on the page: no row, no label, no control. */
function expectNoFadeOut() {
  expect(markerNames()).toEqual(["Start", "EOM", "Stop"]);
  // The marker's own label only: a hint that mentions a fade-out in a sentence is fine (audit M4).
  expect(screen.queryByText("Fade-out")).toBeNull();
  expect(screen.queryByLabelText("Fade-out")).toBeNull();
}

beforeEach(() => {
  // jsdom has no 2D context, no AudioContext and no ResizeObserver (as in CueEditor.test.tsx).
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  vi.stubGlobal("AudioContext", undefined);
  vi.stubGlobal("webkitAudioContext", undefined);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

describe("LinerCueEditor: opening (LR11)", () => {
  it("a liner opens without a Fade-out row and with its name as the title", async () => {
    serve();
    await openEditor();

    expectNoFadeOut();
    expect(times()).toEqual(AUTOMATIC_TIMES);
    expect(screen.getByText("Automatic")).toBeTruthy();
    expect(screen.queryByText("Manual")).toBeNull();
    // The title line as the song editor shows it: the liner's name, its length, its FORMAT.
    const text = (document.body.textContent ?? "").replace(/\s+/g, " ");
    expect(text).toContain("Station ID · 0:07 · MP3");
    // Everything else is the song editor's: the same columns, nudges and buttons.
    const headers = within(screen.getByRole("table"))
      .getAllByRole("columnheader")
      .map((header) => header.textContent?.trim());
    expect(headers).toEqual(["Marker", "Time", "Nudge", "Automatic was"]);
    for (const name of ["Start", "EOM", "Stop"] as const) {
      const labels = within(markerRow(name))
        .getAllByRole("button")
        .map((nudge) => nudge.textContent?.trim());
      expect(labels).toEqual(["-1", "-0.1", "-0.01", "+0.01", "+0.1", "+1"]);
    }
    expect(button("Save")).toBeTruthy();
    expect(button("Reset to automatic")).toBeTruthy();
  });

  it("asks the liner API for the view, the waveform and L1's audio, with the token", async () => {
    const api = serve();
    await openEditor();

    await waitFor(() => {
      const paths = api.calls.map((c) => `${c.method} ${c.path}`);
      expect(paths).toContain(`GET ${CUES_PATH}`);
      expect(paths).toContain(`GET ${WAVEFORM_PATH}`);
      expect(paths).toContain(`GET ${AUDIO_PATH}`);
    });
    for (const call of api.calls) {
      expect(call.token).toBe("dev-token");
      expect(call.path.startsWith("/api/v1/library")).toBe(false);
    }
  });

  it("opens on a saved edit with the Manual badge, still without a Fade-out row", async () => {
    serve({ manual: MANUAL });
    await openEditor();

    expect(times()).toEqual(MANUAL_TIMES);
    expect(screen.getByText("Manual")).toBeTruthy();
    expectNoFadeOut();
  });
});

describe("LinerCueEditor: editing and saving (LR11)", () => {
  it("Save for a liner sends start, eom and stop only", async () => {
    const api = serve();
    await openEditor();
    fireEvent.click(nudgeButton("EOM", "+0.1"));

    expect(button("Save").disabled).toBe(false);
    fireEvent.click(button("Save"));

    await waitFor(() => expect(api.puts()).toHaveLength(1));
    const put = api.puts()[0];
    expect(put.path).toBe(CUES_PATH);
    expect(put.token).toBe("dev-token");
    // The liner API never takes a fade: no fade_at_ms key at all.
    expect(put.body).toStrictEqual({ start_ms: 120, eom_ms: 6_700, stop_ms: 6_900 });
    // The editor is read again and stays on the saved edit.
    await waitFor(() => expect(api.editorReads().length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(screen.getByText("Manual")).toBeTruthy());
    expect(times()).toEqual({ ...AUTOMATIC_TIMES, eom: "0:06.700" });
  });

  it("nudging Stop keeps the Fade-out hidden and flips the badge to Manual", async () => {
    const api = serve();
    await openEditor();

    // Fade-out follows Stop, so Stop can move below where the fade was (0:06.900).
    fireEvent.click(nudgeButton("Stop", "-0.1"));

    expect(times()).toEqual({ ...AUTOMATIC_TIMES, stop: "0:06.800" });
    expect(screen.getByText("Manual")).toBeTruthy();
    expect(screen.queryByText("Automatic")).toBeNull();
    expectNoFadeOut();

    // Back to autocue's Stop: the fade came back with it, so the badge reads Automatic again.
    fireEvent.click(nudgeButton("Stop", "+0.1"));
    expect(times()).toEqual(AUTOMATIC_TIMES);
    expect(screen.getByText("Automatic")).toBeTruthy();

    // Stop stops at the end of the liner (0:07.000), and on the way down at EOM (0:06.600), as
    // in the song editor; Save sends the Stop shown.
    fireEvent.click(nudgeButton("Stop", "+1"));
    expect(times().stop).toBe("0:07.000");
    fireEvent.click(nudgeButton("Stop", "-1"));
    expect(times()).toEqual({ ...AUTOMATIC_TIMES, stop: "0:06.600" });
    fireEvent.click(button("Save"));
    await waitFor(() => expect(api.puts()).toHaveLength(1));
    expect(api.puts()[0].body).toStrictEqual({ start_ms: 120, eom_ms: 6_600, stop_ms: 6_600 });
  });

  // Audit I1: Fade-out is hidden on the waveforms too, neither drawn nor grabbed. The panes draw
  // once they have a width and a 2D context, so both are faked here: a context that records the
  // labels it is told to paint, and a 1000 px wide canvas. A liner's End view opens on the whole
  // 7 s (0..7_000 ms), so x = ms * 1000 / 7000. With a Fade-out kept at Stop under the hood, a
  // grab on Stop's line would pick the Fade-out (it comes first) and the drag would move nothing.
  it("the waveforms neither draw nor grab a Fade-out: dragging Stop's line moves Stop", async () => {
    const drawn: string[] = [];
    const context = new Proxy({} as Record<string | symbol, unknown>, {
      get: (_target, key) =>
        key === "fillText"
          ? (text: string) => void drawn.push(text)
          : key === "measureText"
            ? () => ({ width: 40 })
            : () => undefined,
      set: () => true,
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      context as unknown as CanvasRenderingContext2D
    );
    vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(1000);
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 1000,
      bottom: 130,
      width: 1000,
      height: 130,
      toJSON: () => ({}),
    } as DOMRect);
    serve();
    await openEditor();
    await waitFor(() => expect(drawn).toContain("Stop"));
    expect(drawn).toContain("EOM");
    expect(drawn).not.toContain("Fade-out");

    const end = screen.getByLabelText("End view");
    const at = (ms: number) => (ms * 1000) / 7_000;
    // clientY 100 is clear of the label strip at the top, so the line (not a label) is grabbed.
    fireEvent(
      end,
      new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: at(6_900), clientY: 100 })
    );
    fireEvent(
      end,
      new MouseEvent("pointermove", { bubbles: true, clientX: at(6_750), clientY: 100 })
    );
    fireEvent(end, new MouseEvent("pointerup", { bubbles: true }));

    expect(times()).toEqual({ ...AUTOMATIC_TIMES, stop: "0:06.750" });
    expect(screen.getByText("Manual")).toBeTruthy();
    expectNoFadeOut();
    expect(drawn).not.toContain("Fade-out");
  });

  it("Reset to automatic sends DELETE", async () => {
    const api = serve({ manual: MANUAL });
    await openEditor();
    expect(screen.getByText("Manual")).toBeTruthy();

    fireEvent.click(button("Reset to automatic"));

    await waitFor(() => expect(api.deletes()).toHaveLength(1));
    expect(api.deletes()[0].path).toBe(CUES_PATH);
    expect(api.deletes()[0].token).toBe("dev-token");
    await waitFor(() => expect(times()).toEqual(AUTOMATIC_TIMES));
    expect(screen.getByText("Automatic")).toBeTruthy();
    expect(screen.queryByText("Manual")).toBeNull();
    await waitFor(() => expect(api.editorReads().length).toBeGreaterThanOrEqual(2));
    expectNoFadeOut();
  });

  it.each(["save", "reset"] as const)(
    "%s marks the station's liner list stale, so its Cues column turns Edited or Auto",
    async (action) => {
      const qc = newClient();
      const listKey = ["station-liners", STATION];
      qc.setQueryData(listKey, { songs_between_liners: null, liners: [] });
      const api = serve({ manual: action === "reset" ? MANUAL : null });
      await openEditor(qc);
      expect(qc.getQueryState(listKey)?.isInvalidated).toBe(false);

      if (action === "save") {
        fireEvent.click(nudgeButton("EOM", "+0.1"));
        fireEvent.click(button("Save"));
        await waitFor(() => expect(api.puts()).toHaveLength(1));
      } else {
        fireEvent.click(button("Reset to automatic"));
        await waitFor(() => expect(api.deletes()).toHaveLength(1));
      }

      await waitFor(() => expect(qc.getQueryState(listKey)?.isInvalidated).toBe(true));
    }
  );
});

describe("LinerCueEditor: a silent liner (LR4, LR11)", () => {
  it("a silent liner shows the note and disables Save", async () => {
    const api = serve({ analysis: "silent", manual: MANUAL });
    await openEditor();

    // Change something first, so a Save that is only disabled "while nothing changed" fails.
    fireEvent.click(nudgeButton("EOM", "+0.1"));

    expect(screen.getByText(LINER_SILENT_NOTE)).toBeTruthy();
    expect(screen.queryByText(SONG_SILENT_NOTE)).toBeNull();
    expect(button("Save").disabled).toBe(true);
    expect(button("Reset to automatic").disabled).toBe(false);

    fireEvent.click(button("Save"));
    expect(api.puts()).toHaveLength(0);
  });

  it.each(["measured", "failed", "waiting"] as const)(
    "a %s liner shows no silent note and can be saved",
    async (analysis) => {
      serve({ analysis });
      await openEditor();

      expect(screen.queryByText(LINER_SILENT_NOTE)).toBeNull();
      fireEvent.click(nudgeButton("EOM", "+0.1"));
      expect(button("Save").disabled).toBe(false);
    }
  );
});

describe("LinerCueEditor: a liner that cannot be opened (LR11)", () => {
  it("a 404 shows the server's reason and the back button", async () => {
    serve({ editorError: { status: 404, detail: NO_SUCH_LINER } });
    const { container } = renderPage(["/previous", PAGE_URL], 1);

    expect(await screen.findByText(NO_SUCH_LINER)).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(container.querySelector("canvas")).toBeNull();

    // Back goes one step back in the history.
    fireEvent.click(button("Back to the liners"));
    expect(await screen.findByText("The page before")).toBeTruthy();
    expect(screen.queryByText(NO_SUCH_LINER)).toBeNull();
  });

  it("Back to the liners goes to the station's page when there is no history", async () => {
    serve({ editorError: { status: 404, detail: NO_SUCH_LINER } });
    renderPage([PAGE_URL]);
    await screen.findByText(NO_SUCH_LINER);

    fireEvent.click(button("Back to the liners"));

    expect(await screen.findByText("The station page")).toBeTruthy();
  });
});
