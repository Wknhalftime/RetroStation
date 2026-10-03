// @vitest-environment jsdom
// PR G2, Task 9 (traceability VC6; rows D91, S3): the listener limit beside the suggested
// maximum. D91: "A limit above the suggestion is allowed, with a warning." The suggestion is
// the live meter row's (D90: the one "stream_resources" progress row); with streaming off there
// is no meter and so no suggestion (questions-g, "Decided in the plan").
// No G2 copy here is spec-named: the warning is a readable alert that names the suggested
// number, and the control that fills it in is a button with a loose name.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn(), apiUpload: vi.fn() };
});

import { apiFetch } from "@/api/client";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { MaxListenersSetting } from "./MaxListenersSetting";

const mockedApiFetch = vi.mocked(apiFetch);
const SETTINGS_URL = "/api/v1/streaming/settings";
const PUT_URL = "/api/v1/streaming/max-sessions";
const LIMIT = { name: /listeners/i };
const SAVE = { name: /save/i };
const USE_SUGGESTED = { name: /suggest|recommend/i };

function meterTask(suggested: number): TaskInfo {
  return {
    task_id: "stream_resources",
    task_type: "stream_resources",
    status: "running",
    progress_data: {
      open_streams: 1,
      scope: "audio_engines",
      streams: [{ cpu_percent: 13.8, memory_mb: 75.0, warming: false }],
      cost: { cpu_percent: 13.8, memory_mb: 75.0, source: "measured" },
      machine: { threads: 16, memory_total_mb: 65430, memory_available_mb: 40100, cpu_percent: 9 },
      suggested_max: suggested,
      limited_by: "processor",
      budget: { cpu_share: 0.5, memory_share: 0.25 },
    },
    started_at: "2026-10-02T09:00:00Z",
    updated_at: "2026-10-02T09:10:00Z",
    completed_at: null,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function puts() {
  return mockedApiFetch.mock.calls.filter(([url]) => url === PUT_URL);
}

function input(): HTMLInputElement {
  return screen.getByRole("spinbutton", LIMIT) as HTMLInputElement;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === SETTINGS_URL)
      return Promise.resolve({
        streaming: "on",
        max_sessions: 3,
        max_sessions_problem: null,
        sign_off: null,
        sign_off_problem: null,
      });
    if (url === PUT_URL) return Promise.resolve({ max_sessions: 80 });
    return Promise.reject(new Error(`unexpected ${url}`));
  });
  act(() => useProgressStore.getState().setTasks([]));
});

describe("MaxListenersSetting with a suggestion", () => {
  it("VC6: warns above the suggested limit; Use suggested fills it in", async () => {
    // With streaming off there is no meter row, so no suggestion and no warning.
    const off = render(<MaxListenersSetting />, { wrapper });
    await screen.findByDisplayValue("3");
    fireEvent.change(input(), { target: { value: "80" } });
    fireEvent.click(screen.getByRole("button", SAVE));
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", USE_SUGGESTED)).toBeNull();
    off.unmount();
    mockedApiFetch.mockClear();

    act(() => useProgressStore.getState().setTasks([meterTask(57)]));
    render(<MaxListenersSetting />, { wrapper });
    await screen.findByDisplayValue("3");
    expect(screen.queryByRole("alert")).toBeNull();

    // At the suggestion: no warning.
    fireEvent.change(input(), { target: { value: "57" } });
    fireEvent.click(screen.getByRole("button", SAVE));
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(screen.queryByRole("alert")).toBeNull();

    // Above it: allowed (saved as given), with a warning that names the suggestion.
    fireEvent.change(input(), { target: { value: "80" } });
    fireEvent.click(screen.getByRole("button", SAVE));
    await waitFor(() => expect(puts()).toHaveLength(2));
    expect(puts()[1]).toEqual([PUT_URL, { method: "PUT", body: JSON.stringify({ value: 80 }) }]);
    const warning = await screen.findByRole("alert");
    expect(warning.textContent ?? "").toMatch(/(?<!\d)(?<!\d\.)57(?!\.?\d)/);

    // Use suggested fills the suggestion in; the warning goes.
    fireEvent.click(screen.getByRole("button", USE_SUGGESTED));
    await waitFor(() => expect(input().value).toBe("57"));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
