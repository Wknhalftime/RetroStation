// @vitest-environment jsdom
// Module B: the SPA's "Radio" link to the backend's public listening pages (plan-f2.md Task 6;
// traceability-f2.md; spec D71).
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { API_BASE } from "@/api/client";
import { Sidebar } from "./Sidebar";

describe("Sidebar", () => {
  // B1 — D71 (`/radio` is served by the backend, not the SPA router)
  it("the sidebar links to the backend's radio page in a new tab", () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>
    );
    const link = screen.getByRole("link", { name: "Radio" });
    expect(link.getAttribute("href")).toBe(`${API_BASE}/radio`);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")?.split(/\s+/)).toContain("noopener");
  });
});
