// @vitest-environment jsdom
// PR G1 loading-state fix: while GET /api/v1/streaming/settings is still in flight, the real
// state (a clip, or none) is not known yet. The section must not claim there is no clip — it
// shows a neutral loading state instead, and renders neither Remove nor Upload, so neither can
// act on data that has not arrived.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn(), apiUpload: vi.fn() };
});

import { apiFetch } from "@/api/client";
import { SignOffClip } from "./SignOffClip";

const mockedApiFetch = vi.mocked(apiFetch);
const NO_CLIP = /no\b.*clip/i;

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("SignOffClip while loading", () => {
  it("shows a loading state, not 'No sign-off clip is set', while settings are pending", () => {
    // Keep GET /settings pending for the whole test: it never resolves here.
    mockedApiFetch.mockImplementation(() => new Promise(() => undefined));
    const { container } = render(<SignOffClip />, { wrapper });

    expect(container.textContent ?? "").not.toMatch(NO_CLIP);
    expect(screen.getByRole("status")).toBeTruthy();

    // Neither control exists yet, so neither can act on data that has not arrived.
    expect(screen.queryByLabelText(/upload a sign-off clip/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /remove/i })).toBeNull();
  });
});
