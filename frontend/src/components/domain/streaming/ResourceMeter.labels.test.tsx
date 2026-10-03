// @vitest-environment jsdom
// PR G2 final review M3 and M5 (not a locked test).
// M3: a stream's processor figure is a share of one processor thread, the machine's is a share
// of the whole machine; the page says which is which, so 13% per stream and 9% for the machine
// are never read as the same unit.
// M5: a running meter row whose data cannot be read says so; it does not claim streaming is off.
import { describe, it, expect, beforeEach } from "vitest";
import { act, render } from "@testing-library/react";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { ResourceMeter } from "./ResourceMeter";

function meterTask(data: Record<string, unknown>): TaskInfo {
  return {
    task_id: "stream_resources",
    task_type: "stream_resources",
    status: "running",
    progress_data: data,
    started_at: "2026-10-03T09:00:00Z",
    updated_at: "2026-10-03T09:10:00Z",
    completed_at: null,
  };
}

const DATA = {
  open_streams: 1,
  scope: "audio_engines",
  streams: [{ cpu_percent: 13.0, memory_mb: 75.0, warming: false }],
  cost: { cpu_percent: 13.0, memory_mb: 75.0, source: "measured" },
  machine: { threads: 16, memory_total_mb: 65430, memory_available_mb: 40100, cpu_percent: 9.0 },
  suggested_max: 61,
  limited_by: "processor",
  budget: { cpu_share: 0.5, memory_share: 0.25 },
};

function show(tasks: TaskInfo[]) {
  act(() => useProgressStore.getState().setTasks(tasks));
}

/** The smallest element whose own text contains ``pattern``. */
function smallestWith(container: HTMLElement, pattern: RegExp): string {
  const matches = Array.from(container.querySelectorAll("*")).filter((el) =>
    pattern.test(el.textContent ?? "")
  );
  const smallest = matches.filter((el) => !matches.some((o) => o !== el && el.contains(o)));
  expect(smallest.length).toBeGreaterThan(0);
  return smallest[0]?.textContent ?? "";
}

beforeEach(() => {
  show([]);
});

describe("ResourceMeter labels", () => {
  it("M3: per-stream CPU is of one processor thread; the machine's is of the whole machine", () => {
    const view = render(<ResourceMeter />);
    show([meterTask(DATA)]);
    const perStream = smallestWith(view.container, /open stream/i);
    expect(perStream).toMatch(/13%\s+of one processor thread/i);
    const engine = smallestWith(view.container, /Engine 1/);
    expect(engine).toMatch(/13%\s+of one (processor )?thread/i);
    const machine = smallestWith(view.container, /Machine:/);
    expect(machine).toMatch(/9%\s+of the whole machine/i);
  });

  it("M5: a running row that cannot be read says the meter data could not be read", () => {
    const view = render(<ResourceMeter />);
    show([meterTask({ open_streams: "two", scope: "audio_engines" })]);
    const text = view.container.textContent ?? "";
    expect(text).toMatch(/could not be read/i);
    expect(text).not.toMatch(/while streaming is off/i);
  });

  it("M5: with no row the page still says the meter runs while streaming is on", () => {
    const view = render(<ResourceMeter />);
    const text = view.container.textContent ?? "";
    expect(text).toMatch(/streaming is on/i);
    expect(text).not.toMatch(/could not be read/i);
  });
});
