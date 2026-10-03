// @vitest-environment jsdom
// PR G2, Task 9 fix round (coordinator review): a stored-limit problem (D27) and the live
// suggestion warning (D91, VC6) are two different reasons to warn the user, but they must
// never become two competing role="alert" elements at once. This covers the case where both
// apply at the same time: one alert region lists both messages.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
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
const PROBLEM = "user_settings.stream_max_sessions must be a whole number >= 1, got '0'";

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

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((url: string) =>
    url === SETTINGS_URL
      ? Promise.resolve({
          streaming: "on",
          max_sessions: null,
          max_sessions_problem: PROBLEM,
          sign_off: null,
          sign_off_problem: null,
        })
      : Promise.reject(new Error(`unexpected ${url}`))
  );
  act(() => useProgressStore.getState().setTasks([meterTask(10)]));
});

describe("MaxListenersSetting with a stored problem and a live suggestion", () => {
  it("collapses both messages into a single alert region", async () => {
    render(<MaxListenersSetting />, { wrapper });
    await screen.findByText(PROBLEM);

    const input = screen.getByRole("spinbutton", { name: /listeners/i }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "15" } }); // above the suggested 10

    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
    const text = alerts[0]?.textContent ?? "";
    expect(text).toMatch(PROBLEM);
    expect(text).toMatch(/(?<!\d)(?<!\d\.)10(?!\.?\d)/);
  });
});
