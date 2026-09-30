// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";
import { PurgeMissingSetting } from "./PurgeMissingSetting";

const mockedApiFetch = vi.mocked(apiFetch);

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((url: string) =>
    Promise.resolve(url === "/api/v1/settings" ? { "library.purge_missing": "never" } : undefined)
  );
});

describe("PurgeMissingSetting", () => {
  it("stores after_scan when chosen", async () => {
    render(<PurgeMissingSetting />, { wrapper });
    const select = await screen.findByRole("combobox", { name: "After a full scan" });

    fireEvent.change(select, { target: { value: "after_scan" } });

    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith("/api/v1/settings/library.purge_missing", {
        method: "PUT",
        body: JSON.stringify({ value: "after_scan" }),
      })
    );
  });

  it("shows never when the setting is unset", async () => {
    mockedApiFetch.mockImplementation(() => Promise.resolve({}));
    render(<PurgeMissingSetting />, { wrapper });

    const select = (await screen.findByRole("combobox", {
      name: "After a full scan",
    })) as HTMLSelectElement;
    expect(select.value).toBe("never");
  });
});
