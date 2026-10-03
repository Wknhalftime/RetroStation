// @vitest-environment jsdom
// G1 final review, P8: the settings are refetched when the sign-off clip is uploaded or
// removed. That refetch must not wipe a listener limit the user has typed but not saved; only
// a change to the stored limit replaces the field.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn(), apiUpload: vi.fn() };
});

import { apiFetch } from "@/api/client";
import { streamSettingsKey, useStreamSettings } from "@/api/streaming";
import { MaxListenersSetting } from "./MaxListenersSetting";

const mockedApiFetch = vi.mocked(apiFetch);
const SETTINGS_URL = "/api/v1/streaming/settings";
const LIMIT = { name: /listeners/i };

const served: { maxSessions: number; clip: string | null } = { maxSessions: 3, clip: null };

function serve() {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === SETTINGS_URL)
      return Promise.resolve({
        streaming: "on",
        max_sessions: served.maxSessions,
        max_sessions_problem: null,
        sign_off: served.clip ? { name: served.clip, seconds: 12.5, format: "mp3" } : null,
        sign_off_problem: null,
      });
    return Promise.reject(new Error(`unexpected ${url}`));
  });
}

/** Shows the clip's name, so the test can see when a refetch has rendered. */
function ClipName() {
  const { data } = useStreamSettings();
  return <p>clip: {data?.sign_off?.name ?? "none"}</p>;
}

function renderWithClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MaxListenersSetting />
      <ClipName />
    </QueryClientProvider>
  );
  return qc;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  served.maxSessions = 3;
  served.clip = null;
  serve();
});

describe("MaxListenersSetting refetch", () => {
  it("keeps an unsaved typed limit when the settings are refetched for the clip", async () => {
    const qc = renderWithClient();
    const input = (await screen.findByRole("spinbutton", LIMIT)) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("3"));
    fireEvent.change(input, { target: { value: "9" } });

    served.clip = "Good night";
    await qc.invalidateQueries({ queryKey: streamSettingsKey });
    await screen.findByText("clip: Good night");

    expect(input.value).toBe("9");
  });

  it("shows a changed stored limit after a refetch", async () => {
    const qc = renderWithClient();
    const input = (await screen.findByRole("spinbutton", LIMIT)) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("3"));

    served.maxSessions = 6;
    await qc.invalidateQueries({ queryKey: streamSettingsKey });

    await waitFor(() => expect(input.value).toBe("6"));
  });
});
