// @vitest-environment jsdom
// PR G1, Task 5 (traceability VB1; row S1): Settings links to the new Streaming page
// (Delivery row G, "Settings page: max listeners"; plan: Settings -> Streaming at
// /settings/streaming).
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn(() => Promise.resolve({})) }));

import { Settings } from "./Settings";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Settings", () => {
  it("VB1: links to the Streaming page", async () => {
    render(<Settings />, { wrapper });
    const link = await screen.findByRole("link", { name: /streaming/i });
    expect(link.getAttribute("href")).toBe("/settings/streaming");
  });
});
