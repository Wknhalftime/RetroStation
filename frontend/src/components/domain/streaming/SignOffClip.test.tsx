// @vitest-environment jsdom
// PR G1, Task 5 (traceability VD1-VD7; rows S5/D26, PG3, M4, I2, I3, M2): the sign-off clip
// on the Streaming page. D26: the user's own clip plays after the last song; without one the
// stream ends there. PG3 and M4: FLAC, MP3 or WAV, up to 25 MB (25 x 2^20 bytes), 1 second to
// 5 minutes; a 5-minute WAV is too big, so long clips should be MP3 or FLAC. The plan's route
// table: POST /api/v1/streaming/sign-off (multipart "file") answers 413, 415, 422 or 503 with
// a reason; DELETE answers 204. M2: a clip whose file is missing is reported.
// No G1 copy is spec-named: controls are found by role with loose names, the page's own
// messages are checked as states and facts, and the server's reasons are shown as given.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn(), apiUpload: vi.fn() };
});

import { ApiError, ServerError, ValidationError, apiFetch, apiUpload } from "@/api/client";
import { SignOffClip } from "./SignOffClip";

const mockedApiFetch = vi.mocked(apiFetch);
const mockedApiUpload = vi.mocked(apiUpload);
const SETTINGS_URL = "/api/v1/streaming/settings";
const SIGN_OFF_URL = "/api/v1/streaming/sign-off";
const CLIP = { name: "Good night", seconds: 12.5, format: "mp3" };
const MAX_CLIP_BYTES = 25 * 2 ** 20;
const FILE_INPUT = /sign-off/i;
const REMOVE = { name: /remove/i };
const NO_CLIP = /no\b.*clip/i;

/** What GET /settings serves now; DELETE clears the clip, as the server does. */
const served: { signOff: typeof CLIP | null; problem: string | null } = {
  signOff: null,
  problem: null,
};

function serve(signOff: typeof CLIP | null, problem: string | null = null) {
  served.signOff = signOff;
  served.problem = problem;
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (url === SETTINGS_URL)
      return Promise.resolve({
        streaming: "on",
        max_sessions: 3,
        max_sessions_problem: null,
        sign_off: served.signOff,
        sign_off_problem: served.problem,
      });
    if (url === SIGN_OFF_URL && options?.method === "DELETE") {
      served.signOff = null;
      return Promise.resolve(undefined);
    }
    return Promise.reject(new Error(`unexpected ${url}`));
  });
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function audioFile(name: string, size?: number): File {
  const file = new File([new Uint8Array([82, 73, 70, 70])], name, { type: "audio/wav" });
  if (size !== undefined) Object.defineProperty(file, "size", { value: size });
  return file;
}

async function choose(file: File) {
  const input = await screen.findByLabelText(FILE_INPUT);
  fireEvent.change(input, { target: { files: [file] } });
}

/** The section's visible text, once the settings have loaded. */
async function sectionText(container: HTMLElement): Promise<string> {
  await screen.findByLabelText(FILE_INPUT);
  await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledWith(SETTINGS_URL));
  return container.textContent ?? "";
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiUpload.mockReset();
});

describe("SignOffClip", () => {
  it("VD1: with no clip, says there is none and the stream ends after the last song", async () => {
    serve(null);
    const { container } = render(<SignOffClip />, { wrapper });
    await waitFor(() => expect(container.textContent ?? "").toMatch(NO_CLIP));
    expect(container.textContent ?? "").toMatch(/last song/i);
    expect(screen.queryByRole("button", REMOVE)).toBeNull();
  });

  it("VD2: shows the clip's name, length and format; Remove deletes it", async () => {
    serve(CLIP);
    const { container } = render(<SignOffClip />, { wrapper });
    await waitFor(() => expect(container.textContent ?? "").toMatch(/Good night/));
    const text = container.textContent ?? "";
    expect(text).toMatch(/12\.5|0:1[23]/);
    expect(text).toMatch(/mp3/i);
    fireEvent.click(screen.getByRole("button", REMOVE));
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(SIGN_OFF_URL, { method: "DELETE" })
    );
    // The page shows the result: the clip is gone.
    await waitFor(() => expect(container.textContent ?? "").toMatch(NO_CLIP));
    expect(container.textContent ?? "").not.toMatch(/Good night/);
  });

  it("VD3: states the rules: formats, size, length, and MP3 or FLAC for long clips", async () => {
    serve(null);
    const { container } = render(<SignOffClip />, { wrapper });
    const text = await sectionText(container);
    const facts = [/FLAC/, /MP3/, /WAV/, /25\s*Mi?B/i, /\b1\s*s(ec(ond)?)?\b/i, /5\s*min/i];
    for (const fact of facts) {
      expect(text).toMatch(fact);
    }
    expect(text).toMatch(/MP3 or FLAC|FLAC or MP3/i);
    expect(text).toMatch(/long/i);
  });

  it("VD4: shows the server's reason when an upload is refused", async () => {
    serve(null);
    const refusals: [Error, string][] = [
      [new ApiError(415, "the clip is Ogg Vorbis; use FLAC, MP3 or WAV"), "Ogg Vorbis"],
      [new ApiError(413, "the clip is larger than 25 MB"), "larger than 25 MB"],
      [
        new ValidationError([
          {
            loc: ["body", "file"],
            msg: "the clip lasts 0.5 s; use 1 second to 5 minutes",
            type: "value_error",
          },
        ]),
        "lasts 0.5 s",
      ],
      [new ServerError(503, "the clip could not be stored: no space left"), "could not be stored"],
    ];
    for (const [error, reason] of refusals) {
      mockedApiUpload.mockRejectedValueOnce(error);
      const view = render(<SignOffClip />, { wrapper });
      await choose(audioFile("clip.wav"));
      await waitFor(() => expect(screen.getByRole("alert").textContent).toContain(reason));
      view.unmount();
    }
  });

  it("VD5: uploads the chosen file as multipart form data and shows the new clip", async () => {
    serve(null);
    mockedApiUpload.mockImplementation(() => {
      served.signOff = CLIP;
      return Promise.resolve(CLIP);
    });
    const { container } = render(<SignOffClip />, { wrapper });
    const file = audioFile("Good night.mp3");
    await choose(file);
    await waitFor(() => expect(mockedApiUpload).toHaveBeenCalledTimes(1));
    const [url, body] = mockedApiUpload.mock.calls[0];
    expect(url).toBe(SIGN_OFF_URL);
    expect(body.get("file")).toBe(file);
    // The page shows the result: the new clip.
    await waitFor(() => expect(container.textContent ?? "").toMatch(/Good night/));
  });

  it("VD6: refuses a file over 25 MB before uploading it; exactly 25 MB is sent", async () => {
    serve(null);
    const view = render(<SignOffClip />, { wrapper });
    await choose(audioFile("long.wav", MAX_CLIP_BYTES + 1));
    expect((await screen.findByRole("alert")).textContent).toMatch(/25\s*Mi?B/i);
    expect(mockedApiUpload).not.toHaveBeenCalled();
    view.unmount();

    mockedApiUpload.mockResolvedValue(CLIP);
    render(<SignOffClip />, { wrapper });
    await choose(audioFile("exact.wav", MAX_CLIP_BYTES));
    await waitFor(() => expect(mockedApiUpload).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("VD7: shows the server's report that the clip's file is missing", async () => {
    // The problem text is the server's data (M2), shown as given, beside the clip.
    serve(CLIP, "the clip's file is missing; upload it again");
    const { container } = render(<SignOffClip />, { wrapper });
    expect(await screen.findByText("the clip's file is missing; upload it again")).toBeTruthy();
    expect(container.textContent ?? "").toMatch(/Good night/);
  });
});
