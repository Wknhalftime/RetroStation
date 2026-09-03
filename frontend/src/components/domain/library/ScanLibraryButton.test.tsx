// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "@/api/client";
import { ScanLibraryButton } from "./ScanLibraryButton";

const mockedApiFetch = vi.mocked(apiFetch);
const LIBRARY_PATH = "D:\\Media\\Music";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function scanCalls(): unknown[][] {
  return mockedApiFetch.mock.calls.filter(([url]) => url === "/api/v1/library/scan");
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((url: string) =>
    url === "/api/v1/settings"
      ? Promise.resolve({ local_path_prefix: LIBRARY_PATH })
      : Promise.resolve(undefined)
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function renderEnabledButton(): Promise<HTMLElement> {
  render(<ScanLibraryButton />, { wrapper });
  const button = await screen.findByRole("button", { name: /Scan Library/ });
  await waitFor(() => expect(button).not.toHaveProperty("disabled", true));
  return button;
}

describe("ScanLibraryButton full-scan guard", () => {
  it("does not start a scan when the operator cancels the confirm", async () => {
    // A full scan reads every byte of every file; on a large library that is
    // hours of I/O. It must never fire from a stray click.
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const button = await renderEnabledButton();

    fireEvent.click(button);

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(scanCalls()).toHaveLength(0);
  });

  it("starts the scan when the operator confirms", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const button = await renderEnabledButton();

    fireEvent.click(button);

    await waitFor(() => expect(scanCalls()).toHaveLength(1));
    const [, init] = scanCalls()[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ root_path: LIBRARY_PATH });
  });

  it("names the library path in the confirm so the operator knows what is about to be read", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const button = await renderEnabledButton();

    fireEvent.click(button);

    const message = vi.mocked(window.confirm).mock.calls[0][0];
    expect(message).toContain(LIBRARY_PATH);
    expect(message).toMatch(/automatically/);
  });
});
