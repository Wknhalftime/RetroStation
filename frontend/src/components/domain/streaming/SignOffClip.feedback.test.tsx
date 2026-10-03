// @vitest-environment jsdom
// G1 final review: I2, a Remove the server refuses (503) shows its reason; M6, the page says
// the clip is uploading while the upload is pending.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn(), apiUpload: vi.fn() };
});

import { ServerError, apiFetch, apiUpload } from "@/api/client";
import { SignOffClip } from "./SignOffClip";

const mockedApiFetch = vi.mocked(apiFetch);
const mockedApiUpload = vi.mocked(apiUpload);
const SETTINGS_URL = "/api/v1/streaming/settings";
const SIGN_OFF_URL = "/api/v1/streaming/sign-off";
const CLIP = { name: "Good night", seconds: 12.5, format: "mp3" };

function serve(refuseDelete: boolean) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (url === SETTINGS_URL)
      return Promise.resolve({
        streaming: "on",
        max_sessions: 3,
        max_sessions_problem: null,
        sign_off: CLIP,
        sign_off_problem: null,
      });
    if (url === SIGN_OFF_URL && options?.method === "DELETE") {
      return refuseDelete
        ? Promise.reject(new ServerError(503, "the sign-off setting could not be saved"))
        : Promise.resolve(undefined);
    }
    return Promise.reject(new Error(`unexpected ${url}`));
  });
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiUpload.mockReset();
});

describe("SignOffClip feedback", () => {
  it("shows the server's reason when a Remove fails", async () => {
    serve(true);
    render(<SignOffClip />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: /remove/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("could not be saved");
    expect(screen.getByText(/Good night/)).toBeTruthy();
  });

  it("says the clip is uploading while the upload is pending, then stops", async () => {
    serve(false);
    let finish: (value: typeof CLIP) => void = () => undefined;
    mockedApiUpload.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        })
    );
    render(<SignOffClip />, { wrapper });
    const input = await screen.findByLabelText(/sign-off/i);
    const file = new File([new Uint8Array([82, 73, 70, 70])], "clip.wav", { type: "audio/wav" });
    fireEvent.change(input, { target: { files: [file] } });

    expect((await screen.findByRole("status")).textContent).toMatch(/uploading/i);
    finish(CLIP);
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });
});
