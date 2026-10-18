// @vitest-environment jsdom
// Cue editor PR 3, Task 11 (R8: the editor page; R7: the requests it sends; ruling I1: silent
// audio). The page is exercised through the DOM and the requests it sends: `fetch` is stubbed
// with a small fake of the PR 2 API (backend/routers/library/cues.py), so the real client,
// schemas and hooks run. Canvas drawing is not tested (jsdom has no 2D context; the pane skips
// drawing on a null context), and neither is the preview's Web Audio scheduling.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { CueEditor } from "./CueEditor";

const FILE_ID = "22222222-2222-4222-8222-222222222222";
const API = `/api/v1/library/files/${FILE_ID}`;
const CUES_PATH = `${API}/cues`;
const WAVEFORM_PATH = `${API}/waveform`;
const AUDIO_PATH = `${API}/audio`;
const PAGE_URL = `/library/files/${FILE_ID}/cues`;

interface ApiMarkers {
  start_ms: number;
  eom_ms: number;
  fade_at_ms: number;
  stop_ms: number;
}

// Heretic Anthem as the brief gives it: autocue's start 0, EOM and Fade-out together at 4:01.100,
// Stop at 4:07.100, in a 250 s file.
const AUTOMATIC: ApiMarkers = {
  start_ms: 0,
  eom_ms: 241_100,
  fade_at_ms: 241_100,
  stop_ms: 247_100,
};
// A saved edit: Start 0:02.000, EOM 4:00.000, Fade-out 3:59.500, Stop 4:06.000.
const MANUAL: ApiMarkers = {
  start_ms: 2_000,
  eom_ms: 240_000,
  fade_at_ms: 239_500,
  stop_ms: 246_000,
};
const VIEW_DURATION_MS = 250_000;
// The waveform's own length can differ from the view's (ledger, Task 6): shorter here, so a page
// that uses it for the timeline would show 4:09 where the view says 4:10.
const WAVEFORM_DURATION_MS = 249_000;
const PEAKS_B64 = btoa(String.fromCharCode(...new Array<number>(100).fill(128)));

const NOT_FINGERPRINTED = "This file hasn't been fingerprinted yet, so its cues can't be edited.";
const NOT_IN_LIBRARY = "This song is not in the library.";
const SILENT_NOTE = "Measured silent — the radio skips this song, so its cues can't be edited.";

// ---------------------------------------------------------------------------
// A fake of the PR 2 API
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
  saveRejection?: string;
  audio?: "ok" | "pending";
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
          file_id: FILE_ID,
          title: "Heretic Anthem",
          artist: "Slipknot",
          format: "flac",
          duration_ms: VIEW_DURATION_MS,
          analysis: options.analysis ?? "measured",
          automatic: AUTOMATIC,
          manual,
        });
      }
      if (path === CUES_PATH && method === "PUT") {
        if (options.saveRejection) {
          return json(422, {
            detail: [{ loc: ["body"], msg: options.saveRejection, type: "value_error" }],
          });
        }
        manual = body as ApiMarkers;
        return new Response(null, { status: 204 });
      }
      if (path === CUES_PATH && method === "DELETE") {
        manual = null;
        return new Response(null, { status: 204 });
      }
      if (path === WAVEFORM_PATH && method === "GET") {
        return json(200, {
          duration_ms: WAVEFORM_DURATION_MS,
          per_second: 10,
          peaks: PEAKS_B64,
        });
      }
      if (path === AUDIO_PATH && method === "GET") {
        if (options.audio === "pending") return new Promise<Response>(() => undefined);
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

function renderPage(entries: string[] = [PAGE_URL], initialIndex?: number) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={entries} initialIndex={initialIndex}>
        {children}
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(
    <Routes>
      <Route path="/library/files/:file_id/cues" element={<CueEditor />} />
      <Route path="/library/artists/a1/works/w1" element={<p>The song page</p>} />
    </Routes>,
    { wrapper }
  );
}

async function openEditor() {
  const view = renderPage();
  await screen.findByRole("table");
  return view;
}

/** The table row for a marker: the row whose first cell is the marker's name. */
function markerRow(name: "Start" | "EOM" | "Fade-out" | "Stop"): HTMLElement {
  const row = screen
    .getAllByRole("row")
    .find((candidate) => candidate.children[0]?.textContent?.trim() === name);
  if (!row) throw new Error(`no table row for ${name}`);
  return row;
}

function cellText(row: HTMLElement, index: number): string {
  return row.children[index]?.textContent?.trim() ?? "";
}

/** The Time column (second cell) of each marker's row. */
function times() {
  return {
    start: cellText(markerRow("Start"), 1),
    eom: cellText(markerRow("EOM"), 1),
    fadeOut: cellText(markerRow("Fade-out"), 1),
    stop: cellText(markerRow("Stop"), 1),
  };
}

/** The "Automatic was" column (fourth cell) of each marker's row. */
function automaticWas() {
  return {
    start: cellText(markerRow("Start"), 3),
    eom: cellText(markerRow("EOM"), 3),
    fadeOut: cellText(markerRow("Fade-out"), 3),
    stop: cellText(markerRow("Stop"), 3),
  };
}

function nudgeButton(marker: "Start" | "EOM" | "Fade-out" | "Stop", label: string) {
  return within(markerRow(marker)).getByRole("button", { name: label });
}

function button(name: string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

const AUTOMATIC_TIMES = {
  start: "0:00.000",
  eom: "4:01.100",
  fadeOut: "4:01.100",
  stop: "4:07.100",
};

beforeEach(() => {
  // jsdom has no 2D context (the pane must skip drawing), no AudioContext (the preview is
  // never ready) and no ResizeObserver (stubbed so a pane may measure itself).
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

describe("CueEditor: opening (R8)", () => {
  it("opens on automatic markers with the Automatic badge", async () => {
    serve();
    await openEditor();

    expect(times()).toEqual(AUTOMATIC_TIMES);
    expect(screen.getByText("Automatic")).toBeTruthy();
    expect(screen.queryByText("Manual")).toBeNull();
  });

  it("opens on manual markers with the Manual badge and shows Automatic was", async () => {
    serve({ manual: MANUAL });
    await openEditor();

    expect(times()).toEqual({
      start: "0:02.000",
      eom: "4:00.000",
      fadeOut: "3:59.500",
      stop: "4:06.000",
    });
    expect(automaticWas()).toEqual(AUTOMATIC_TIMES);
    expect(screen.getByText("Manual")).toBeTruthy();
    expect(screen.queryByText("Automatic")).toBeNull();
  });

  it("badge rule: a saved edit equal to automatic reads Automatic", async () => {
    serve({ manual: AUTOMATIC });
    await openEditor();

    expect(screen.getByText("Automatic")).toBeTruthy();
    expect(screen.queryByText("Manual")).toBeNull();
  });

  it("shows the title line from the view: title, its duration as m:ss, and the FORMAT", async () => {
    serve();
    await openEditor();

    // The view says 250 s (4:10); the waveform says 249 s (4:09).
    expect(screen.getByText("Heretic Anthem · 4:10 · FLAC")).toBeTruthy();
  });

  it("lays out the marker table: headers, four marker rows, six nudge buttons each", async () => {
    serve();
    await openEditor();

    const headers = within(screen.getByRole("table"))
      .getAllByRole("columnheader")
      .map((header) => header.textContent?.trim());
    expect(headers).toEqual(["Marker", "Time", "Nudge", "Automatic was"]);

    for (const name of ["Start", "EOM", "Fade-out", "Stop"] as const) {
      const labels = within(markerRow(name))
        .getAllByRole("button")
        .map((nudge) => nudge.textContent?.trim());
      expect(labels).toEqual(["-1", "-0.1", "-0.01", "+0.01", "+0.1", "+1"]);
    }
  });

  it("offers Play from Start, Play End cue, stop buttons, Save and Reset to automatic", async () => {
    serve();
    await openEditor();

    expect(button("▶ Play from Start")).toBeTruthy();
    expect(button("▶ Play End cue")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "■" })).toHaveLength(2);
    expect(button("Save")).toBeTruthy();
    expect(button("Reset to automatic")).toBeTruthy();
  });

  it("draws the whole-song strip and the two close-ups", async () => {
    serve();
    const { container } = await openEditor();

    await waitFor(() => expect(container.querySelectorAll("canvas")).toHaveLength(3));
  });

  it("asks the API for this file's view, waveform and audio, with the token", async () => {
    const api = serve();
    await openEditor();

    await waitFor(() => {
      const paths = api.calls.map((c) => `${c.method} ${c.path}`);
      expect(paths).toContain(`GET ${CUES_PATH}`);
      expect(paths).toContain(`GET ${WAVEFORM_PATH}`);
      expect(paths).toContain(`GET ${AUDIO_PATH}`);
    });
    for (const call of api.calls) expect(call.token).toBe("dev-token");
  });
});

describe("CueEditor: nudging (R8, R1)", () => {
  it("nudging EOM by +0.1 updates its time and flips the badge to Manual", async () => {
    serve();
    await openEditor();

    fireEvent.click(nudgeButton("EOM", "+0.1"));

    expect(times()).toEqual({ ...AUTOMATIC_TIMES, eom: "4:01.200" });
    expect(screen.getByText("Manual")).toBeTruthy();
    expect(screen.queryByText("Automatic")).toBeNull();
    // The column that remembers autocue does not move.
    expect(automaticWas()).toEqual(AUTOMATIC_TIMES);
  });

  it("nudging back to automatic's value flips the badge back to Automatic", async () => {
    serve();
    await openEditor();

    fireEvent.click(nudgeButton("EOM", "+0.1"));
    fireEvent.click(nudgeButton("EOM", "-0.1"));

    expect(times()).toEqual(AUTOMATIC_TIMES);
    expect(screen.getByText("Automatic")).toBeTruthy();
    expect(screen.queryByText("Manual")).toBeNull();
  });

  it.each([
    ["-1", "4:00.100"],
    ["-0.1", "4:01.000"],
    ["-0.01", "4:01.090"],
    ["+0.01", "4:01.110"],
    ["+0.1", "4:01.200"],
    ["+1", "4:02.100"],
  ])("the %s button moves EOM to %s and nothing else", async (label, expected) => {
    serve();
    await openEditor();

    fireEvent.click(nudgeButton("EOM", label));

    expect(times()).toEqual({ ...AUTOMATIC_TIMES, eom: expected });
  });

  it("stops a nudge at the limit R1 sets: Start cannot go below 0", async () => {
    serve();
    await openEditor();

    fireEvent.click(nudgeButton("Start", "-1"));

    expect(times().start).toBe("0:00.000");
    expect(screen.getByText("Automatic")).toBeTruthy();
  });

  it("stops Stop at the end of the view's duration, not the waveform's", async () => {
    serve();
    await openEditor();

    // 247.100 s + 1 s + 1 s = 249.100 s; one more second would be 250.100 s, past the file.
    fireEvent.click(nudgeButton("Stop", "+1"));
    fireEvent.click(nudgeButton("Stop", "+1"));
    expect(times().stop).toBe("4:09.100");
    fireEvent.click(nudgeButton("Stop", "+1"));

    expect(times().stop).toBe("4:10.000");
  });

  it("does not drag other markers along: pulling Stop below EOM stops at EOM", async () => {
    serve();
    await openEditor();

    for (let i = 0; i < 7; i++) fireEvent.click(nudgeButton("Stop", "-1"));

    // 247.100 s less 7 s would be 240.100 s; Stop cannot pass EOM and Fade-out at 241.100 s.
    expect(times()).toEqual({ ...AUTOMATIC_TIMES, stop: "4:01.100" });
  });
});

describe("CueEditor: Save (R8, R7)", () => {
  it("Save sends PUT with the displayed markers in API names", async () => {
    const api = serve();
    await openEditor();
    fireEvent.click(nudgeButton("EOM", "+0.1"));

    expect(button("Save").disabled).toBe(false);
    fireEvent.click(button("Save"));

    await waitFor(() => expect(api.puts()).toHaveLength(1));
    const put = api.puts()[0];
    expect(put.path).toBe(CUES_PATH);
    expect(put.token).toBe("dev-token");
    expect(put.body).toEqual({
      start_ms: 0,
      eom_ms: 241_200,
      fade_at_ms: 241_100,
      stop_ms: 247_100,
    });
  });

  it("after Save the editor is read again and the page stays on the saved edit", async () => {
    const api = serve();
    await openEditor();
    fireEvent.click(nudgeButton("EOM", "+0.1"));
    fireEvent.click(button("Save"));

    await waitFor(() => expect(api.editorReads().length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(screen.getByText("Manual")).toBeTruthy());
    expect(times()).toEqual({ ...AUTOMATIC_TIMES, eom: "4:01.200" });
    expect(automaticWas()).toEqual(AUTOMATIC_TIMES);
  });

  it("a 422 on Save shows the server's reason and keeps the displayed markers", async () => {
    const reason = "This fade would leave the song too short to play on the radio.";
    const api = serve({ saveRejection: reason });
    await openEditor();
    fireEvent.click(nudgeButton("EOM", "+0.1"));
    fireEvent.click(button("Save"));

    expect(await screen.findByText(reason)).toBeTruthy();
    expect(api.puts()).toHaveLength(1);
    expect(times()).toEqual({ ...AUTOMATIC_TIMES, eom: "4:01.200" });
  });
});

describe("CueEditor: Reset to automatic (R8, R7)", () => {
  it("Reset to automatic sends DELETE and shows automatic values", async () => {
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
  });

  it("Reset discards nudges that were never saved", async () => {
    const api = serve();
    await openEditor();
    fireEvent.click(nudgeButton("EOM", "+1"));
    expect(times().eom).toBe("4:02.100");

    fireEvent.click(button("Reset to automatic"));

    await waitFor(() => expect(api.deletes()).toHaveLength(1));
    await waitFor(() => expect(times()).toEqual(AUTOMATIC_TIMES));
    expect(screen.getByText("Automatic")).toBeTruthy();
  });
});

describe("CueEditor: a song that cannot be opened (R8, R7)", () => {
  it("a 409 shows the not-fingerprinted message and no waveform", async () => {
    serve({ editorError: { status: 409, detail: NOT_FINGERPRINTED } });
    const { container } = renderPage();

    expect(await screen.findByText(NOT_FINGERPRINTED)).toBeTruthy();
    expect(container.querySelector("canvas")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("a 404 shows the server's message", async () => {
    serve({ editorError: { status: 404, detail: NOT_IN_LIBRARY } });
    const { container } = renderPage();

    expect(await screen.findByText(NOT_IN_LIBRARY)).toBeTruthy();
    expect(container.querySelector("canvas")).toBeNull();
  });

  it("offers Back to the song, which goes back one step", async () => {
    serve({ editorError: { status: 409, detail: NOT_FINGERPRINTED } });
    renderPage(["/library/artists/a1/works/w1", PAGE_URL], 1);
    await screen.findByText(NOT_FINGERPRINTED);

    fireEvent.click(screen.getByText("Back to the song"));

    expect(await screen.findByText("The song page")).toBeTruthy();
    expect(screen.queryByText(NOT_FINGERPRINTED)).toBeNull();
  });
});

describe("CueEditor: the audio preview (R8)", () => {
  it("preview buttons are disabled while audio is not ready", async () => {
    // jsdom has no AudioContext, so the preview can never become ready.
    const api = serve();
    await openEditor();
    await waitFor(() => expect(api.calls.map((c) => c.path)).toContain(AUDIO_PATH));

    expect(button("▶ Play from Start").disabled).toBe(true);
    expect(button("▶ Play End cue").disabled).toBe(true);
  });

  it("preview buttons are disabled while the audio is still loading", async () => {
    serve({ audio: "pending" });
    await openEditor();

    expect(button("▶ Play from Start").disabled).toBe(true);
    expect(button("▶ Play End cue").disabled).toBe(true);
  });

  it("says Loading audio… while the audio loads", async () => {
    serve({ audio: "pending" });
    await openEditor();

    expect((await screen.findAllByText("Loading audio…")).length).toBeGreaterThan(0);
  });
});

describe("CueEditor: silent audio (ruling I1)", () => {
  it("shows the note and disables Save while Reset stays enabled", async () => {
    const api = serve({ analysis: "silent", manual: MANUAL });
    const { container } = await openEditor();

    expect(screen.getByText(SILENT_NOTE)).toBeTruthy();
    expect(button("Save").disabled).toBe(true);
    expect(button("Reset to automatic").disabled).toBe(false);
    // The waveform is still shown.
    await waitFor(() => expect(container.querySelectorAll("canvas").length).toBeGreaterThan(0));

    fireEvent.click(button("Save"));
    expect(api.puts()).toHaveLength(0);
  });

  it("Reset still works on silent audio", async () => {
    const api = serve({ analysis: "silent", manual: MANUAL });
    await openEditor();

    fireEvent.click(button("Reset to automatic"));

    await waitFor(() => expect(api.deletes()).toHaveLength(1));
  });

  it("does not show the note for a song that was measured", async () => {
    serve({ analysis: "measured" });
    await openEditor();

    expect(screen.queryByText(SILENT_NOTE)).toBeNull();
  });
});
