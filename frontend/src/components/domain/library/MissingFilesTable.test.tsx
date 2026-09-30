// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MissingFilesTable } from "./MissingFilesTable";
import type { MissingFile } from "@/lib/schemas/missing";

const file = (id: string, name: string, overrides: Partial<MissingFile> = {}): MissingFile => ({
  id,
  file_path: `D:\\Music\\${name}`,
  artist_name: "Prince",
  track_title: "Kiss",
  release_title: "Parade",
  missing_since: "2026-09-27T10:00:00Z",
  work_id: "w1",
  work_title: "Kiss",
  match_count: 1,
  work_has_present_file: true,
  candidates: [],
  ...overrides,
});

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";

describe("MissingFilesTable", () => {
  it("toggles one row, and every row of the page", () => {
    const onToggle = vi.fn();
    const onToggleAll = vi.fn();
    render(
      <MissingFilesTable
        files={[file(A, "a.flac"), file(B, "b.flac")]}
        selected={new Set([A])}
        onToggle={onToggle}
        onToggleAll={onToggleAll}
        onRemap={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /b\.flac/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select every file on this page" }));

    expect(onToggle).toHaveBeenCalledWith(B);
    expect(onToggleAll).toHaveBeenCalledOnce();
    expect((screen.getByRole("checkbox", { name: /a\.flac/ }) as HTMLInputElement).checked).toBe(
      true
    );
  });

  it("flags a work that has no copy left on disk", () => {
    render(
      <MissingFilesTable
        files={[file(A, "a.flac", { work_has_present_file: false })]}
        selected={new Set()}
        onToggle={vi.fn()}
        onToggleAll={vi.fn()}
        onRemap={vi.fn()}
      />
    );

    expect(screen.getByText("no copy on disk")).toBeTruthy();
  });
});
