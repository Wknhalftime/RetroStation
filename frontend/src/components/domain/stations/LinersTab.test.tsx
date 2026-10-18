// @vitest-environment jsdom
// PR L1, Task 6 (traceability LR1, LR2, LR3, LR5; Review Focus 1): the Liners tab on the station
// dashboard, mockup A. It talks to the Task 5 routes under /api/v1/stations/{station_id}/liners.
//
// `fetch` is stubbed at the network edge (not apiFetch), so the tests see the real requests:
// method, path, JSON body, and the multipart `file` of each upload. The stub is a small stateful
// server: an upload that is "added" adds a liner, rename and delete change the list, and the next
// GET returns the new list, the way the real routes do.
//
// Choices the brief leaves open, pinned here (see tests-frontend-report.md):
//  - delete asks with window.confirm("Delete liner {name}?"), like StationList does;
//  - rename asks for the new name with window.prompt;
//  - a "path order" folder upload sorts the files by their relative path;
//  - Preview is observed through HTMLMediaElement.prototype.play (jsdom offers no other signal),
//    so an <audio autoPlay> build would not satisfy it.
//
// Fidelity to real browsers, so the tests cannot pass on a page that only works in jsdom:
//  - a file from a plain <input type=file> has webkitRelativePath === "" (plainFile);
//  - a file read from a dropped folder's entries also has webkitRelativePath === "": its path
//    is only known from the walk (entry.fullPath);
//  - once the drop event has been dispatched, dataTransfer.items and .files are empty, so a page
//    that waits (await) before reading them loses the drop;
//  - FileSystemDirectoryReader.readEntries returns a few entries per call and [] at the end.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { LinersTab } from "./LinersTab";

const STATION = "11111111-1111-4111-8111-111111111111";
const LINERS_PATH = `/api/v1/stations/${STATION}/liners`;
const CADENCE_PATH = `${LINERS_PATH}/cadence`;
const HINT = "Blank turns liners off. 1 to 1,000.";

// ---------------------------------------------------------------------------
// A stateful stand-in for the Task 5 routes
// ---------------------------------------------------------------------------

type LinerRow = {
  id: string;
  name: string;
  format: "flac" | "mp3" | "wav";
  duration_ms: number;
  cues: "edited" | "auto" | "failed" | "analysing" | "silent";
  uploaded_at: string;
};

type Library = { songs_between_liners: number | null; liners: LinerRow[] };

type Reply = { status: number; json?: unknown };
type UploadResult = "added" | "duplicate" | Reply;

type Recorded = { method: string; path: string; body: string | null; file: string | null };

function liner(id: string, name: string, durationMs: number, cues: LinerRow["cues"]): LinerRow {
  return {
    id,
    name,
    format: "mp3",
    duration_ms: durationMs,
    cues,
    uploaded_at: "2026-10-17T10:00:00Z",
  };
}

// The real backend answers a jpg, a txt or a cue sheet 422 (probe_clip -> UnreadableClipError,
// loc ["body", "file"]); a 415 is a recognised but unsupported audio type.
const UNREADABLE_REASON = "the file is not audio that can be read";
const NOT_READABLE: Reply = {
  status: 422,
  json: { detail: [{ loc: ["body", "file"], msg: UNREADABLE_REASON, type: "value_error" }] },
};
const NOT_SUPPORTED: Reply = { status: 415, json: { detail: "not a supported audio file" } };
const NOT_AUDIO_EXTENSIONS = [".jpg", ".txt", ".cue"];

function baseName(fileName: string): string {
  return fileName.split(/[\\/]/).pop() ?? fileName;
}

function toResponse(reply: Reply): Response {
  if (reply.json === undefined) return new Response(null, { status: reply.status });
  return new Response(JSON.stringify(reply.json), {
    status: reply.status,
    headers: { "Content-Type": "application/json" },
  });
}

function stem(fileName: string): string {
  const dot = fileName.lastIndexOf(".");
  return dot > 0 ? fileName.slice(0, dot) : fileName;
}

type Server = {
  calls: Recorded[];
  state: Library;
  /** Requests with this method whose path ends with `suffix`. */
  where: (method: string, suffix: string) => Recorded[];
  /** The names of the files POSTed so far, in the order the requests started. */
  uploaded: () => string[];
  /** The most POST requests that were ever open at the same moment. */
  maxInFlight: () => number;
};

function serve(
  library: Library,
  onUpload: (file: File, index: number) => UploadResult | Promise<UploadResult> = (file) =>
    NOT_AUDIO_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext))
      ? NOT_READABLE
      : "added"
): Server {
  const state: Library = JSON.parse(JSON.stringify(library)) as Library;
  const calls: Recorded[] = [];
  let inFlight = 0;
  let peak = 0;
  let uploads = 0;
  let minted = 0;

  const handler = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input));
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.pathname;
    const body = init?.body;
    const record: Recorded = {
      method,
      path,
      body: typeof body === "string" ? body : null,
      file: null,
    };
    calls.push(record);

    if (path === LINERS_PATH && method === "GET") return toResponse({ status: 200, json: state });

    if (path === CADENCE_PATH && method === "PUT") {
      const sent = JSON.parse(record.body ?? "{}") as { songs_between_liners: number | null };
      state.songs_between_liners = sent.songs_between_liners;
      return toResponse({ status: 200, json: state });
    }

    if (path === LINERS_PATH && method === "POST") {
      const entry = (body as FormData).get("file") as File;
      // The page may send the bare name or the relative path as the part's filename; the real
      // backend keeps only the last path part either way (shared upload reader).
      const sentAs = baseName(entry.name);
      record.file = sentAs;
      const index = uploads++;
      inFlight++;
      peak = Math.max(peak, inFlight);
      const result = await onUpload(entry, index);
      inFlight--;
      if (result === "added") {
        const id = `new-${++minted}`;
        state.liners.push(liner(id, stem(sentAs), 4000, "analysing"));
        return toResponse({ status: 201, json: { id, outcome: "added" } });
      }
      if (result === "duplicate") {
        return toResponse({ status: 201, json: { id: "existing", outcome: "duplicate" } });
      }
      return toResponse(result);
    }

    const one = path.startsWith(`${LINERS_PATH}/`) ? path.slice(LINERS_PATH.length + 1) : null;
    if (one !== null && method === "PATCH") {
      const sent = JSON.parse(record.body ?? "{}") as { name: string };
      const target = state.liners.find((l) => l.id === one);
      if (target) target.name = sent.name;
      return toResponse({ status: 204 });
    }
    if (one !== null && method === "DELETE") {
      state.liners = state.liners.filter((l) => l.id !== one);
      return toResponse({ status: 204 });
    }
    if (one !== null && one.endsWith("/audio") && method === "GET") {
      return new Response("audio-bytes", {
        status: 200,
        headers: { "Content-Type": "audio/mpeg" },
      });
    }
    return toResponse({ status: 404, json: { detail: `unexpected ${method} ${path}` } });
  };

  vi.stubGlobal("fetch", vi.fn(handler));

  return {
    calls,
    state,
    where: (method, suffix) => calls.filter((c) => c.method === method && c.path.endsWith(suffix)),
    uploaded: () => calls.filter((c) => c.method === "POST").map((c) => c.file ?? ""),
    maxInFlight: () => peak,
  };
}

// ---------------------------------------------------------------------------
// Rendering and interaction helpers
// ---------------------------------------------------------------------------

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const ONE_LINER: Library = {
  songs_between_liners: null,
  liners: [liner("l1", "Station ID", 8000, "auto")],
};

async function openTab(library: Library = ONE_LINER) {
  const server = serve(library);
  const view = render(<LinersTab stationId={STATION} />, { wrapper });
  // The list has loaded once it shows a liner, or says there are none.
  if (library.liners.length > 0) await screen.findByText(library.liners[0].name);
  else await screen.findByText("No liners yet.");
  return { server, ...view };
}

function row(name: string): HTMLElement {
  const found = screen.getAllByRole("row").find((r) => within(r).queryByText(name) !== null);
  if (!found) throw new Error(`no table row for ${name}`);
  return found;
}

function cadenceBox(): HTMLInputElement {
  return screen.getByLabelText("Songs between liners") as HTMLInputElement;
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
}

function pickedFile(relativePath: string): File {
  const name = relativePath.split("/").pop() ?? relativePath;
  const file = new File(["audio"], name, { type: "audio/mpeg" });
  Object.defineProperty(file, "webkitRelativePath", { value: relativePath });
  return file;
}

// What a plain <input type=file> gives: a file with no relative path.
function plainFile(name: string): File {
  const file = new File(["audio"], name, { type: "audio/mpeg" });
  Object.defineProperty(file, "webkitRelativePath", { value: "" });
  return file;
}

function asFileList(files: File[]): FileList {
  return Object.assign([...files], {
    item: (i: number) => files[i] ?? null,
  }) as unknown as FileList;
}

function chooseFolder(container: HTMLElement, files: File[]) {
  const input = container.querySelector("input[webkitdirectory]");
  if (!input) throw new Error("no hidden folder input with webkitdirectory");
  fireEvent.change(input, { target: { files: asFileList(files) } });
}

function chooseFiles(container: HTMLElement, files: File[]) {
  const input = container.querySelector("input[type=file]:not([webkitdirectory])");
  if (!input) throw new Error("no hidden file input");
  fireEvent.change(input, { target: { files: asFileList(files) } });
}

// ---------------------------------------------------------------------------
// Drag and drop: a stand-in for DataTransfer and the FileSystem entry API
// ---------------------------------------------------------------------------

type FakeFileEntry = {
  isFile: true;
  isDirectory: false;
  name: string;
  fullPath: string;
  file: (onFile: (file: File) => void) => void;
};

type FakeDirEntry = {
  isFile: false;
  isDirectory: true;
  name: string;
  fullPath: string;
  createReader: () => { readEntries: (onEntries: (batch: FakeEntry[]) => void) => void };
};

type FakeEntry = FakeFileEntry | FakeDirEntry;

type Tree = File | { folder: string; children: Tree[] };

// Entries as a browser gives them: the File has no relative path (the walk knows it), and a
// directory reader hands out at most `batch` entries per call, then [] at the end.
function toEntry(node: Tree, parent: string, batch: number): FakeEntry {
  if (node instanceof File) {
    return {
      isFile: true,
      isDirectory: false,
      name: node.name,
      fullPath: `${parent}/${node.name}`,
      file: (onFile) => onFile(plainFile(node.name)),
    };
  }
  const fullPath = `${parent}/${node.folder}`;
  const children = node.children.map((child) => toEntry(child, fullPath, batch));
  return {
    isFile: false,
    isDirectory: true,
    name: node.folder,
    fullPath,
    createReader: () => {
      let next = 0;
      return {
        readEntries: (onEntries) => {
          const served = children.slice(next, next + batch);
          next += batch;
          onEntries(served);
        },
      };
    },
  };
}

// `dropped` are the top-level things the user dragged: loose files and/or folders.
function dropOn(target: Element, dropped: Tree[], batch = 2) {
  const entries = dropped.map((node) => toEntry(node, "", batch));
  // The top-level "files" of a dropped folder is the folder itself, as an empty file.
  const topFiles = dropped.map((node) =>
    node instanceof File ? plainFile(node.name) : plainFile(node.folder)
  );
  let open = true;
  const items = entries.map((entry, i) => ({
    kind: "file",
    type: entry.isFile ? "audio/mpeg" : "",
    // Browsers hand these out only while the drop event is being dispatched.
    webkitGetAsEntry: () => (open ? entry : null),
    getAsFile: () => (open ? topFiles[i] : null),
  }));
  const dataTransfer = {
    types: ["Files"],
    dropEffect: "none",
    get files() {
      return open ? asFileList(topFiles) : asFileList([]);
    },
    get items() {
      return open ? Object.assign([...items], { length: items.length }) : [];
    },
  };
  // A drop is only delivered if dragover was cancelled; otherwise the browser would open the
  // file in the tab instead.
  const notCancelled = fireEvent.dragOver(target, { dataTransfer });
  expect(notCancelled).toBe(false);
  fireEvent.drop(target, { dataTransfer });
  open = false;
}

// Clicks on file inputs, whether the link is a <label> or a button that calls input.click().
function watchFilePickers() {
  const clicked: { folder: boolean }[] = [];
  const listener = (event: Event) => {
    const target = event.target;
    if (target instanceof HTMLInputElement && target.type === "file") {
      clicked.push({ folder: target.hasAttribute("webkitdirectory") });
    }
  };
  document.addEventListener("click", listener, true);
  return {
    clicked,
    stop: () => document.removeEventListener("click", listener, true),
  };
}

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

beforeEach(() => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "prompt").mockReturnValue(null);
});

afterEach(() => {
  Reflect.deleteProperty(URL, "createObjectURL");
  Reflect.deleteProperty(URL, "revokeObjectURL");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// The list (LR1, LR3, LR5)
// ---------------------------------------------------------------------------

describe("LinersTab", () => {
  it("shows the cadence, the liners and their cue labels", async () => {
    const library: Library = {
      songs_between_liners: 3,
      liners: [
        liner("l1", "Station ID", 8000, "auto"),
        liner("l2", "Sweeper", 65000, "edited"),
        liner("l3", "More Music", 125000, "failed"),
        liner("l4", "Fresh", 5000, "analysing"),
        liner("l5", "Quiet", 3000, "silent"),
      ],
    };
    const { server } = await openTab(library);

    // LR1: the cadence box shows the stored value, with its placeholder, Save and hint.
    await waitFor(() => expect(cadenceBox().value).toBe("3"));
    expect(cadenceBox().placeholder).toBe("blank = off");
    expect(saveButton()).toBeTruthy();
    expect(screen.getByText(HINT)).toBeTruthy();

    // LR2, LR5: the drop zone and its two links.
    expect(screen.getByText(/Drop liner files or a folder here/)).toBeTruthy();
    expect(screen.getByText("choose files")).toBeTruthy();
    expect(screen.getByText("choose folder")).toBeTruthy();

    // LR5: the table headers.
    for (const header of ["Liner", "Length", "Cues"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeTruthy();
    }

    // Each liner's name, its length as m:ss, and its cue label, in its own row.
    const expected: [string, string, string][] = [
      ["Station ID", "0:08", "Auto"],
      ["Sweeper", "1:05", "Edited"],
      ["More Music", "2:05", "Auto (analysis failed)"],
      ["Fresh", "0:05", "Analysing…"],
      ["Quiet", "0:03", "Silent: never plays"],
    ];
    for (const [name, length, cues] of expected) {
      const line = within(row(name));
      expect(line.getByText(length)).toBeTruthy();
      expect(line.getByText(cues)).toBeTruthy();
      expect(line.getByRole("button", { name: "Preview" })).toBeTruthy();
      expect(line.getByRole("button", { name: "Rename" })).toBeTruthy();
      expect(line.getByRole("button", { name: "Delete" })).toBeTruthy();
    }
    expect(screen.queryByText("No liners yet.")).toBeNull();

    // L3 (LR11): each row links Edit cues to that liner's cue editor page.
    for (const { id, name } of library.liners) {
      const link = within(row(name)).getByRole("link", { name: "Edit cues" });
      expect(link.getAttribute("href")).toBe(`/stations/${STATION}/liners/${id}/cues`);
    }

    // The list is read from the station's liners route.
    expect(server.where("GET", LINERS_PATH).length).toBeGreaterThan(0);
  });

  it("refetches the liners when ['station-liners', stationId] is invalidated", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const server = serve({
      songs_between_liners: 3,
      liners: [liner("l1", "Station ID", 8000, "auto")],
    });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <LinersTab stationId={STATION} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    await screen.findByText("Station ID");
    const before = server.where("GET", LINERS_PATH).length;

    // The cue editor (L3) invalidates this key after a save.
    server.state.liners.push(liner("l2", "Sweeper", 5000, "analysing"));
    await act(async () => {
      await qc.invalidateQueries({ queryKey: ["station-liners", STATION] });
    });

    expect(await screen.findByText("Sweeper")).toBeTruthy();
    expect(server.where("GET", LINERS_PATH).length).toBe(before + 1);
  });

  it("shows No liners yet. for an empty library", async () => {
    await openTab({ songs_between_liners: null, liners: [] });
    expect(screen.getByText("No liners yet.")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Liner" })).toBeTruthy();
    expect(cadenceBox().value).toBe("");
  });

  // -------------------------------------------------------------------------
  // The cadence (LR1)
  // -------------------------------------------------------------------------

  it("Save sends null for a blank box and 3 for '3'", async () => {
    const { server } = await openTab({
      songs_between_liners: 5,
      liners: [liner("l1", "Station ID", 8000, "auto")],
    });
    await waitFor(() => expect(cadenceBox().value).toBe("5"));

    fireEvent.change(cadenceBox(), { target: { value: "" } });
    fireEvent.click(saveButton());
    await waitFor(() => expect(server.where("PUT", "/cadence")).toHaveLength(1));
    expect(JSON.parse(server.where("PUT", "/cadence")[0].body ?? "")).toEqual({
      songs_between_liners: null,
    });

    fireEvent.change(cadenceBox(), { target: { value: "3" } });
    fireEvent.click(saveButton());
    await waitFor(() => expect(server.where("PUT", "/cadence")).toHaveLength(2));
    const second = server.where("PUT", "/cadence")[1];
    expect(second.path).toBe(CADENCE_PATH);
    // A whole number is sent as an int, not the string "3".
    expect(JSON.parse(second.body ?? "")).toEqual({ songs_between_liners: 3 });
  });

  it("Save is disabled for 0, 1001 and 2.5", async () => {
    const { server } = await openTab({
      songs_between_liners: 5,
      liners: [liner("l1", "Station ID", 8000, "auto")],
    });
    await waitFor(() => expect(cadenceBox().value).toBe("5"));

    for (const bad of ["0", "1001", "2.5"]) {
      fireEvent.change(cadenceBox(), { target: { value: bad } });
      expect(saveButton().disabled).toBe(true);
      // The hint stays on screen to say what is allowed.
      expect(screen.getByText(HINT)).toBeTruthy();
      fireEvent.click(saveButton());
    }
    await settle();
    expect(server.where("PUT", "/cadence")).toHaveLength(0);

    // The ends of the range are valid.
    for (const good of ["1", "1000"]) {
      fireEvent.change(cadenceBox(), { target: { value: good } });
      expect(saveButton().disabled).toBe(false);
    }
  });

  // -------------------------------------------------------------------------
  // Upload (LR2, Review Focus 1)
  // -------------------------------------------------------------------------

  it("a folder with a jpg skips it, names it, and uploads the rest", async () => {
    const { server, container } = await openTab({ songs_between_liners: null, liners: [] });

    chooseFolder(container, [
      pickedFile("Jingles/a.mp3"),
      pickedFile("Jingles/cover.jpg"),
      pickedFile("Jingles/notes.txt"),
      pickedFile("Jingles/sub/b.mp3"),
    ]);

    expect(await screen.findByText("Added 2 · Already there 0 · Skipped 2")).toBeTruthy();
    // Each skipped file is named by its path in the folder, with the server's own reason
    // (the real backend answers a non-audio file 422, with the reason in detail[0].msg).
    expect(screen.getByText(`Jingles/cover.jpg: ${UNREADABLE_REASON}`)).toBeTruthy();
    expect(screen.getByText(`Jingles/notes.txt: ${UNREADABLE_REASON}`)).toBeTruthy();
    // Every file is sent: the server, not the page, decides what is audio. Subfolders included.
    expect(server.uploaded()).toEqual(["a.mp3", "cover.jpg", "notes.txt", "b.mp3"]);
    // The two audio files are now in the library (the list is refreshed).
    expect(await screen.findByText("a")).toBeTruthy();
    expect(await screen.findByText("b")).toBeTruthy();
    expect(screen.queryByText("No liners yet.")).toBeNull();
  });

  it("uploads a folder's files in path order", async () => {
    const { server, container } = await openTab({ songs_between_liners: null, liners: [] });

    chooseFolder(container, [
      pickedFile("Jingles/zeta.mp3"),
      pickedFile("Jingles/alpha/one.mp3"),
      pickedFile("Jingles/beta.mp3"),
    ]);

    await screen.findByText("Added 3 · Already there 0 · Skipped 0");
    expect(server.uploaded()).toEqual(["one.mp3", "beta.mp3", "zeta.mp3"]);
  });

  it("uploads one file at a time", async () => {
    let release: (() => void) | undefined;
    const firstDone = new Promise<void>((resolve) => {
      release = resolve;
    });
    const server = serve(
      { songs_between_liners: null, liners: [] },
      async (_file, index): Promise<UploadResult> => {
        if (index === 0) await firstDone;
        return "added";
      }
    );
    const { container } = render(<LinersTab stationId={STATION} />, { wrapper });
    await screen.findByText("No liners yet.");

    chooseFolder(container, [
      pickedFile("Jingles/a.mp3"),
      pickedFile("Jingles/b.mp3"),
      pickedFile("Jingles/c.mp3"),
    ]);

    await waitFor(() => expect(server.uploaded()).toEqual(["a.mp3"]));
    // The first request is still open: the second must not have started.
    await settle();
    expect(server.uploaded()).toEqual(["a.mp3"]);

    release?.();
    await screen.findByText("Added 3 · Already there 0 · Skipped 0");
    expect(server.uploaded()).toEqual(["a.mp3", "b.mp3", "c.mp3"]);
    expect(server.maxInFlight()).toBe(1);
  });

  it("a duplicate counts as Already there", async () => {
    const answers: UploadResult[] = [
      "duplicate",
      { status: 409, json: { detail: "already in this station's library" } },
      "added",
    ];
    const server = serve({ songs_between_liners: null, liners: [] }, (_f, index) => answers[index]);
    const { container } = render(<LinersTab stationId={STATION} />, { wrapper });
    await screen.findByText("No liners yet.");

    chooseFiles(container, [
      plainFile("a-one.mp3"),
      plainFile("b-two.mp3"),
      plainFile("c-three.mp3"),
    ]);

    expect(await screen.findByText("Added 1 · Already there 2 · Skipped 0")).toBeTruthy();
    expect(server.uploaded()).toEqual(["a-one.mp3", "b-two.mp3", "c-three.mp3"]);
    // Already there is a count, not a listed problem: only skipped files get a line.
    expect(screen.queryByText(/^a-one\.mp3:/)).toBeNull();
    expect(screen.queryByText(/^b-two\.mp3:/)).toBeNull();
  });

  it("a 413, 415 or 422 counts as Skipped, with the file named and the server's reason", async () => {
    const answers: UploadResult[] = [
      { status: 413, json: { detail: "liner is too large" } },
      { status: 422, json: { detail: [{ loc: ["body", "file"], msg: "unreadable", type: "x" }] } },
      NOT_SUPPORTED,
      "added",
    ];
    serve({ songs_between_liners: null, liners: [] }, (_f, index) => answers[index]);
    const { container } = render(<LinersTab stationId={STATION} />, { wrapper });
    await screen.findByText("No liners yet.");

    // Plain picked files have no relative path: the line falls back to the file name.
    chooseFiles(container, [
      plainFile("a-big.wav"),
      plainFile("b-bad.mp3"),
      plainFile("c-odd.bin"),
      plainFile("d-ok.mp3"),
    ]);

    expect(await screen.findByText("Added 1 · Already there 0 · Skipped 3")).toBeTruthy();
    expect(screen.getByText("a-big.wav: liner is too large")).toBeTruthy();
    expect(screen.getByText("b-bad.mp3: unreadable")).toBeTruthy();
    expect(screen.getByText("c-odd.bin: not a supported audio file")).toBeTruthy();
    expect(screen.queryByText(/^d-ok\.mp3:/)).toBeNull();
  });

  // -------------------------------------------------------------------------
  // The drop zone and the two links (LR2, LR5)
  // -------------------------------------------------------------------------

  it("dropping files on the drop zone uploads them", async () => {
    const { server } = await openTab({ songs_between_liners: null, liners: [] });
    const zone = screen.getByText(/Drop liner files or a folder here/);

    dropOn(zone, [plainFile("a-one.mp3"), plainFile("b-cover.jpg"), plainFile("c-two.mp3")]);

    expect(await screen.findByText("Added 2 · Already there 0 · Skipped 1")).toBeTruthy();
    expect(server.uploaded()).toEqual(["a-one.mp3", "b-cover.jpg", "c-two.mp3"]);
    expect(screen.getByText(`b-cover.jpg: ${UNREADABLE_REASON}`)).toBeTruthy();
  });

  it("dropping a folder uploads every file in it, subfolders included, in path order", async () => {
    const { server } = await openTab({ songs_between_liners: null, liners: [] });
    const zone = screen.getByText(/Drop liner files or a folder here/);

    // The reader hands out two entries per call, so a page that reads once misses some.
    dropOn(zone, [
      {
        folder: "Jingles",
        children: [
          plainFile("zeta.mp3"),
          plainFile("cover.jpg"),
          {
            folder: "alpha",
            children: [
              plainFile("one.mp3"),
              { folder: "deeper", children: [plainFile("two.mp3")] },
            ],
          },
        ],
      },
    ]);

    expect(await screen.findByText("Added 3 · Already there 0 · Skipped 1")).toBeTruthy();
    expect(server.uploaded()).toEqual(["two.mp3", "one.mp3", "cover.jpg", "zeta.mp3"]);
    // The folder itself is not sent as a file, and a skipped file is named by its path.
    expect(screen.getByText(`Jingles/cover.jpg: ${UNREADABLE_REASON}`)).toBeTruthy();
  });

  it("the links open the file and folder pickers", async () => {
    await openTab({ songs_between_liners: null, liners: [] });
    const watch = watchFilePickers();
    try {
      fireEvent.click(screen.getByText("choose folder"));
      expect(watch.clicked).toEqual([{ folder: true }]);

      fireEvent.click(screen.getByText("choose files"));
      expect(watch.clicked).toEqual([{ folder: true }, { folder: false }]);
    } finally {
      watch.stop();
    }
  });

  // -------------------------------------------------------------------------
  // Rename and delete (LR3)
  // -------------------------------------------------------------------------

  it("rename sends PATCH with the new name", async () => {
    vi.mocked(window.prompt).mockReturnValue("Top of the Hour");
    const { server } = await openTab();

    fireEvent.click(within(row("Station ID")).getByRole("button", { name: "Rename" }));

    await waitFor(() => expect(server.where("PATCH", "/l1")).toHaveLength(1));
    const sent = server.where("PATCH", "/l1")[0];
    expect(sent.path).toBe(`${LINERS_PATH}/l1`);
    expect(JSON.parse(sent.body ?? "")).toEqual({ name: "Top of the Hour" });
    // The list shows the new name once refreshed.
    expect(await screen.findByText("Top of the Hour")).toBeTruthy();
    expect(screen.queryByText("Station ID")).toBeNull();
  });

  it("cancelling a rename sends nothing", async () => {
    const { server } = await openTab();
    vi.mocked(window.prompt).mockReturnValue(null);

    fireEvent.click(within(row("Station ID")).getByRole("button", { name: "Rename" }));

    await settle();
    expect(window.prompt).toHaveBeenCalled();
    expect(server.where("PATCH", "/l1")).toHaveLength(0);
    expect(screen.getByText("Station ID")).toBeTruthy();
  });

  it("delete asks first, then sends DELETE", async () => {
    const { server } = await openTab();
    let deletesWhenAsked = -1;
    vi.mocked(window.confirm).mockImplementation(() => {
      deletesWhenAsked = server.where("DELETE", "/l1").length;
      return true;
    });

    fireEvent.click(within(row("Station ID")).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(server.where("DELETE", "/l1")).toHaveLength(1));
    expect(window.confirm).toHaveBeenCalledWith("Delete liner Station ID?");
    expect(deletesWhenAsked).toBe(0);
    expect(server.where("DELETE", "/l1")[0].path).toBe(`${LINERS_PATH}/l1`);
    // The refreshed list no longer has it.
    expect(await screen.findByText("No liners yet.")).toBeTruthy();
  });

  it("declining the delete confirm sends nothing", async () => {
    const { server } = await openTab();
    vi.mocked(window.confirm).mockReturnValue(false);

    fireEvent.click(within(row("Station ID")).getByRole("button", { name: "Delete" }));

    await settle();
    expect(window.confirm).toHaveBeenCalledWith("Delete liner Station ID?");
    expect(server.where("DELETE", "/l1")).toHaveLength(0);
    expect(screen.getByText("Station ID")).toBeTruthy();
  });

  // -------------------------------------------------------------------------
  // Preview (LR5)
  // -------------------------------------------------------------------------

  it("Preview fetches the audio, plays it from an object URL, and revokes the URL on unmount", async () => {
    const createObjectURL = vi.fn(() => "blob:liner-preview-1");
    const revokeObjectURL = vi.fn();
    // jsdom has neither; they are removed again in afterEach.
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const played: string[] = [];
    vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function (
      this: HTMLMediaElement
    ) {
      played.push(this.src);
      return Promise.resolve();
    });
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);

    const { server, unmount } = await openTab();
    fireEvent.click(within(row("Station ID")).getByRole("button", { name: "Preview" }));

    await waitFor(() => expect(played).toHaveLength(1));
    expect(server.where("GET", "/l1/audio")).toHaveLength(1);
    expect(server.where("GET", "/l1/audio")[0].path).toBe(`${LINERS_PATH}/l1/audio`);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(played[0]).toBe("blob:liner-preview-1");
    expect(revokeObjectURL).not.toHaveBeenCalled();

    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:liner-preview-1");
  });
});
