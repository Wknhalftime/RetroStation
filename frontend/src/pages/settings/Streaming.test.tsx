// @vitest-environment jsdom
// PR G1, Task 5 (traceability VE1, VE2; rows S1, S21/D34, PG1, H9): the Streaming settings
// page. D34 and PG1: streaming is on, off or unavailable; STREAM_ENABLED stays in .env, so the
// page shows the state read-only and names the setting to change. H9: the page fails per
// section, never as a whole.
// No G1 copy is spec-named: the state is read from the page's status element by its word,
// and an error is checked as a readable alert, never as an exact sentence.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn(), apiUpload: vi.fn() };
});

import { ServerError, apiFetch } from "@/api/client";
import { Streaming } from "./Streaming";

const mockedApiFetch = vi.mocked(apiFetch);
const SETTINGS_URL = "/api/v1/streaming/settings";
const STATES = ["on", "off", "unavailable"] as const;

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function serve(streaming: (typeof STATES)[number]) {
  mockedApiFetch.mockImplementation((url: string) =>
    url === SETTINGS_URL
      ? Promise.resolve({
          streaming,
          max_sessions: 3,
          max_sessions_problem: null,
          sign_off: null,
          sign_off_problem: null,
        })
      : Promise.reject(new Error(`unexpected ${url}`))
  );
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("Streaming", () => {
  it("VE1: shows streaming on, off or unavailable, read-only, naming STREAM_ENABLED", async () => {
    const shown: string[] = [];
    for (const state of STATES) {
      serve(state);
      const view = render(<Streaming />, { wrapper });
      const status = await screen.findByRole("status");
      const others = STATES.filter((s) => s !== state);
      await screen.findByText(new RegExp(`\\b${state}\\b`, "i"), { selector: "[role=status]" });
      for (const other of others) {
        expect(status.textContent ?? "").not.toMatch(new RegExp(`\\b${other}\\b`, "i"));
      }
      shown.push(status.textContent ?? "");
      // PG1: the page names the setting the user changes, and offers no control for it.
      expect(screen.getByText(/STREAM_ENABLED/)).toBeTruthy();
      expect(screen.queryByRole("checkbox")).toBeNull();
      expect(screen.queryByRole("switch")).toBeNull();
      expect(await screen.findByRole("spinbutton", { name: /listeners/i })).toBeTruthy();
      view.unmount();
    }
    expect(new Set(shown).size).toBe(STATES.length);
  });

  it("VE2: a section that cannot load says so and the page still renders", async () => {
    mockedApiFetch.mockRejectedValue(new ServerError(503, "the database is not answering"));
    render(<Streaming />, { wrapper });
    expect(await screen.findByRole("heading", { name: /streaming/i })).toBeTruthy();
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
    for (const alert of alerts) {
      expect((alert.textContent ?? "").trim().length).toBeGreaterThan(0);
    }
  });
});
