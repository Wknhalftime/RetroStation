// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn(() => Promise.resolve({})) }));

import { PurgeMissingSetting } from "./PurgeMissingSetting";

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

describe("PurgeMissingSetting warning", () => {
  it("describes the choice as permanent, run after every full scan, releasing plays", async () => {
    render(<PurgeMissingSetting />, { wrapper });
    const select = await screen.findByRole("combobox", { name: "After a full scan" });

    const warning = select.getAttribute("aria-describedby");
    const text = document.getElementById(warning ?? "")?.textContent ?? "";
    expect(text).toMatch(/every full scan/);
    expect(text).toMatch(/cannot be undone/);
    expect(text).toMatch(/back to review/);
    expect(screen.getByRole("option", { name: /permanently delete/ })).toBeTruthy();
  });
});
