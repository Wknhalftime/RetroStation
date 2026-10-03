// @vitest-environment jsdom
// PR G2, Task 9 (traceability VS1; rows D89, D90, I8): the Sidebar's activity dots. D89: a
// running cue run ("cue_analysis") marks Settings, where the Streaming page lives, the way a
// running scan marks Library. D90/I8: the cost meter's "stream_resources" row is a meter, not
// a task: it never marks any link, in any status.
// The dot has no text, so it is found as the element a running task adds to its link: the
// same marker a scan adds to Library.
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { Sidebar } from "./Sidebar";

type Status = "running" | "completed" | "failed";

function task(id: string, type: string, status: Status = "running"): TaskInfo {
  return {
    task_id: id,
    task_type: type,
    status,
    progress_data: {},
    started_at: "2026-10-02T09:00:00Z",
    updated_at: "2026-10-02T09:05:00Z",
    completed_at: status === "running" ? null : "2026-10-02T09:05:00Z",
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

describe("Sidebar dots", () => {
  it("VS1: a running cue run puts a dot on Settings; the meter row never does", () => {
    const idle = allLinks();
    const settingsIdle = link(/settings/i).children.length;
    const libraryIdle = link(/library/i).children.length;

    // The marker a running scan adds to Library.
    setTasks([task("scan-1", "scan")]);
    const library = link(/library/i);
    const marker = library.lastElementChild?.outerHTML;
    expect(library.children.length).toBe(libraryIdle + 1);
    expect(marker).toBeTruthy();
    expect(allLinks()).not.toEqual(idle);

    // A running cue run: Settings gains that same marker; nothing else changes.
    setTasks([task("cue-1", "cue_analysis")]);
    const settings = link(/settings/i);
    expect(settings.children.length).toBe(settingsIdle + 1);
    expect(settings.lastElementChild?.outerHTML).toBe(marker);
    const others = Object.entries(allLinks()).filter(([name]) => !/settings/i.test(name));
    for (const [name, html] of others) expect(html).toBe(idle[name]);

    // The meter row, in every status, alone or beside the cue run: no dot of its own.
    for (const status of ["running", "completed", "failed"] as const) {
      setTasks([task("stream_resources", "stream_resources", status)]);
      expect(allLinks()).toEqual(idle);
    }
    setTasks([task("cue-1", "cue_analysis"), task("stream_resources", "stream_resources")]);
    expect(link(/settings/i).children.length).toBe(settingsIdle + 1);
    expect(link(/library/i).innerHTML).toBe(idle[link(/library/i).textContent ?? ""]);
  });
});
