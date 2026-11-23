// @vitest-environment jsdom
// Phase 4.1 PR 2 (SPEC D4, M10): while the MusicBrainz cache fill runs, Library carries the dot a
// running scan puts on it (the Library link, as 3b's D21 gave its credit run). The names re-check
// the fill hands off (S7) marks the Resolution Center, as the matching re-check does.
// The dot has no text, so it is found as the element a running task adds to its link.
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { Sidebar } from "./Sidebar";

function task(id: string, type: string): TaskInfo {
  return {
    task_id: id,
    task_type: type,
    status: "running",
    progress_data: {},
    started_at: "2026-11-22T09:00:00Z",
    updated_at: "2026-11-22T09:05:00Z",
    completed_at: null,
  };
}

function setTasks(tasks: TaskInfo[]) {
  act(() => useProgressStore.getState().setTasks(tasks));
}

function link(name: RegExp): HTMLElement {
  return screen.getByRole("link", { name });
}

/** Every link's markup, by its text. */
function allLinks(): Record<string, string> {
  return Object.fromEntries(
    screen.getAllByRole("link").map((el) => [el.textContent ?? "", el.innerHTML])
  );
}

beforeEach(() => {
  setTasks([]);
  render(
    <MemoryRouter>
      <Sidebar />
    </MemoryRouter>
  );
});

afterEach(() => {
  cleanup();
  setTasks([]);
});

describe("Sidebar dots: the MusicBrainz cache fill", () => {
  it("D4: a running fill puts the scan's dot on Library, and nowhere else", () => {
    setTasks([task("scan-1", "scan")]);
    const scanning = allLinks();
    setTasks([]);
    const idle = allLinks();

    setTasks([task("fill-1", "mb_cache_fill")]);
    expect(allLinks()).toEqual(scanning);
    expect(allLinks()).not.toEqual(idle);
  });

  it("S7: a names re-check marks the Resolution Center, as the matching re-check does", () => {
    const idle = allLinks();
    const centerIdle = link(/resolution center/i).children.length;
    setTasks([task("recheck-1", "matching_recheck")]);
    const rechecking = allLinks();
    expect(link(/resolution center/i).children.length).toBe(centerIdle + 1);
    setTasks([]);

    setTasks([task("names-1", "names_recheck")]);
    expect(allLinks()).toEqual(rechecking);
    expect(allLinks()).not.toEqual(idle);
  });
});
