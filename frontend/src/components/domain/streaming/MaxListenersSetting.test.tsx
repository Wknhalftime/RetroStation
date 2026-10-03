// @vitest-environment jsdom
// PR G1, Task 5 (traceability VC1-VC5; rows S1, S4/D10, S7/D27): the listener limit on the
// Streaming page. D10: the limit defaults to 3. D27: a limit that is not a whole number >= 1
// is refused on save (the page checks before sending; the server checks again). The plan's
// route table: PUT /api/v1/streaming/max-sessions with {"value": int}.
// No G1 copy is spec-named, so controls are found by role with loose names, and messages are
// checked as states (shown, readable), never as exact sentences.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn(), apiUpload: vi.fn() };
});

import { apiFetch } from "@/api/client";
import { MaxListenersSetting } from "./MaxListenersSetting";

const mockedApiFetch = vi.mocked(apiFetch);
const SETTINGS_URL = "/api/v1/streaming/settings";
const PUT_URL = "/api/v1/streaming/max-sessions";
const LIMIT = { name: /listeners/i };
const SAVE = { name: /save/i };

function settings(maxSessions: number | null, problem: string | null = null) {
  return {
    streaming: "on",
    max_sessions: maxSessions,
    max_sessions_problem: problem,
    sign_off: null,
    sign_off_problem: null,
  };
}

function serve(maxSessions: number | null, problem: string | null = null) {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === SETTINGS_URL) return Promise.resolve(settings(maxSessions, problem));
    if (url === PUT_URL) return Promise.resolve({ max_sessions: 7 });
    return Promise.reject(new Error(`unexpected ${url}`));
  });
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function puts() {
  return mockedApiFetch.mock.calls.filter(([url]) => url === PUT_URL);
}

/** Replace the loaded limit (3) with ``value`` and save. */
async function enter(value: string) {
  await screen.findByDisplayValue("3");
  fireEvent.change(screen.getByRole("spinbutton", LIMIT), { target: { value } });
  fireEvent.click(screen.getByRole("button", SAVE));
}

/** A refusal is shown: an alert with readable text. */
async function expectRefusal() {
  const alert = await screen.findByRole("alert");
  expect((alert.textContent ?? "").trim().length).toBeGreaterThan(0);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("MaxListenersSetting", () => {
  it("VC1: shows the server's limit (the server says 3 when nothing is set)", async () => {
    // The default itself is the server's (D T2.1); the page shows what it is given.
    serve(5);
    render(<MaxListenersSetting />, { wrapper });
    const input = (await screen.findByRole("spinbutton", LIMIT)) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("5"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("VC2: refuses 0 without calling the API", async () => {
    serve(3);
    render(<MaxListenersSetting />, { wrapper });
    await enter("0");
    await expectRefusal();
    expect(puts()).toHaveLength(0);
  });

  it("VC3: refuses a value that is not a whole number of 1 or more", async () => {
    serve(3);
    for (const bad of ["2.5", "-1", ""]) {
      const view = render(<MaxListenersSetting />, { wrapper });
      await enter(bad);
      await expectRefusal();
      view.unmount();
    }
    expect(puts()).toHaveLength(0);
  });

  it("VC4: saves a whole number as an integer", async () => {
    serve(3);
    render(<MaxListenersSetting />, { wrapper });
    await enter("7");
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(PUT_URL, {
        method: "PUT",
        body: JSON.stringify({ value: 7 }),
      })
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("VC5: shows the server's account of a bad stored limit", async () => {
    // The problem text is the server's data (D27), shown as given.
    const problem = "user_settings.stream_max_sessions must be a whole number >= 1, got '0'";
    serve(null, problem);
    render(<MaxListenersSetting />, { wrapper });
    expect(await screen.findByText(problem)).toBeTruthy();
  });
});
