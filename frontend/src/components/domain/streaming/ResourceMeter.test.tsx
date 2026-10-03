// @vitest-environment jsdom
// PR G2, Task 9 (traceability VF1-VF4; rows D90, D91, D95, S2, S3, S21/D34, PG13): the cost
// meter on the Streaming page. D90: the figures travel in the one "stream_resources" progress
// row (through /ws into the progress store), never a route; the page shows the cost per stream,
// the open streams, each engine and the machine. D95: the section is titled "Audio engine cost
// per stream" (spec-named, pinned verbatim) and counts the Liquidsoap engines only. D91: the
// suggested maximum and what sets it; disk is not counted, and the page says so. The PR A
// gate's figures stand in until an engine is measured.
// Everything but the D95 title is found loosely and checked as a state, never as a sentence:
// figures as whole numbers, units as MB or MiB (GB or GiB for the machine), and the resource
// that sets the suggestion as the one named nearest to it (audit SF5-SF7).
import { describe, it, expect, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { ResourceMeter } from "./ResourceMeter";

const TITLE = "Audio engine cost per stream";
const PROCESSOR = /\b(processor|cpu)\b/i;
const MEMORY = /\b(memory|ram)\b/i;
const RESOURCE = /\b(processor|cpu|memory|ram)\b/gi;

type Source = "measured" | "reference";
type Limit = "processor" | "memory";
type Status = "running" | "completed" | "failed";

function meterData(source: Source = "measured", limitedBy: Limit = "processor") {
  return {
    // Three open streams, two engines measured: an engine that exited is left out (T8.8).
    open_streams: 3,
    scope: "audio_engines",
    streams: [
      { cpu_percent: 11.0, memory_mb: 71.0, warming: false },
      { cpu_percent: 15.0, memory_mb: 79.0, warming: false },
    ],
    cost: { cpu_percent: 13.0, memory_mb: 75.0, source },
    machine: { threads: 16, memory_total_mb: 65430, memory_available_mb: 40100, cpu_percent: 9.0 },
    suggested_max: limitedBy === "processor" ? 57 : 23,
    limited_by: limitedBy,
    budget: { cpu_share: 0.5, memory_share: 0.25 },
  };
}

function meterTask(data: Record<string, unknown>, status: Status = "running"): TaskInfo {
  return {
    task_id: "stream_resources",
    task_type: "stream_resources",
    status,
    progress_data: data,
    started_at: "2026-10-02T09:00:00Z",
    updated_at: "2026-10-02T09:10:00Z",
    completed_at: status === "running" ? null : "2026-10-02T09:10:00Z",
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function show(tasks: TaskInfo[]) {
  act(() => useProgressStore.getState().setTasks(tasks));
}

/** Not inside a larger number: no digit, and no "digit." (a decimal), just before. A sentence's
 * full stop may run into the next element's text ("per stream.3 open"). */
const NOT_AFTER_A_NUMBER = "(?<!\\d)(?<!\\d\\.)";

/** ``number`` as a whole number: not inside a larger number or a decimal. */
function whole(number: number): RegExp {
  return new RegExp(`${NOT_AFTER_A_NUMBER}${number}(?!\\.?\\d)`);
}

/** ``number`` followed by ``unit``, allowing one decimal of zero (13, 13.0). */
function figure(number: number, unit: string): RegExp {
  return new RegExp(`${NOT_AFTER_A_NUMBER}${number}(\\.0)?\\s*${unit}`, "i");
}

/** The smallest element whose text has ``number`` and names a resource. */
function suggestionText(container: HTMLElement, number: number): string {
  const resource = new RegExp(RESOURCE.source, "i");
  const word = whole(number);
  const matches = Array.from(container.querySelectorAll("*")).filter((el) => {
    const text = el.textContent ?? "";
    return word.test(text) && resource.test(text);
  });
  const smallest = matches.filter((el) => !matches.some((o) => o !== el && el.contains(o)));
  expect(smallest.length).toBeGreaterThan(0);
  return smallest[0]?.textContent ?? "";
}

/** The resource word nearest to ``number`` in ``text``, by character distance. */
function nearestResource(text: string, number: number): string {
  const at = text.search(whole(number));
  expect(at).toBeGreaterThanOrEqual(0);
  const end = at + String(number).length;
  let nearest = "";
  let best = Infinity;
  for (const found of text.matchAll(RESOURCE)) {
    const start = found.index ?? 0;
    const distance = start >= end ? start - end : at - (start + found[0].length);
    if (distance < best) {
      best = distance;
      nearest = found[0];
    }
  }
  return nearest;
}

beforeEach(() => {
  act(() => useProgressStore.getState().setTasks([]));
});

describe("ResourceMeter", () => {
  it("VF1: titles it Audio engine cost per stream and lists each stream and the machine", () => {
    render(<ResourceMeter />, { wrapper });
    show([meterTask(meterData())]);
    expect(screen.getByRole("heading", { name: TITLE })).toBeTruthy();
    const text = document.body.textContent ?? "";
    // the cost per stream (D95's label names it): the mean processor and memory
    expect(text).toMatch(figure(13, "%"));
    expect(text).toMatch(figure(75, "Mi?B"));
    // the open streams (D90)
    expect(text).toMatch(whole(3));
    // each stream's engine: its processor and its memory
    expect(text).toMatch(figure(11, "%"));
    expect(text).toMatch(figure(15, "%"));
    expect(text).toMatch(figure(71, "Mi?B"));
    expect(text).toMatch(figure(79, "Mi?B"));
    // the machine: its threads and its memory
    const threads = whole(16).source;
    const unit = "(thread|processor|core|cpu)";
    expect(text).toMatch(new RegExp(`${threads}\\D{0,30}${unit}|${unit}\\D{0,30}${threads}`, "i"));
    const binary = "65[,.\\s\\u202f]?430\\s*Mi?B";
    const decimal = "6[345](\\.\\d+)?\\s*Gi?B";
    expect(text).toMatch(new RegExp(`${NOT_AFTER_A_NUMBER}(${binary}|${decimal})`, "i"));
  });

  it("VF2: says when the figures are the engine test's", () => {
    const view = render(<ResourceMeter />, { wrapper });
    const note = /reference|engine test|gate|benchmark|not (yet )?measured/i;
    show([meterTask(meterData("measured"))]);
    expect(view.container.textContent ?? "").not.toMatch(note);
    show([meterTask(meterData("reference"))]);
    expect(view.container.textContent ?? "").toMatch(note);
  });

  it("VF3: shows the suggested limit and what sets it, disk not counted", () => {
    const view = render(<ResourceMeter />, { wrapper });
    show([meterTask(meterData("measured", "processor"))]);
    expect(nearestResource(suggestionText(view.container, 57), 57)).toMatch(PROCESSOR);
    expect(view.container.textContent ?? "").toMatch(/disk/i);

    show([meterTask(meterData("measured", "memory"))]);
    expect(nearestResource(suggestionText(view.container, 23), 23)).toMatch(MEMORY);
  });

  it("VF4: with no meter row, says the meter runs while streaming is on", () => {
    const view = render(<ResourceMeter />, { wrapper });
    // No row, a completed one (shutdown), or a failed one (reaped in an outage): no figures.
    const rows = [[], [meterTask(meterData(), "completed")], [meterTask(meterData(), "failed")]];
    for (const tasks of rows) {
      show(tasks);
      expect(screen.getByRole("heading", { name: TITLE })).toBeTruthy();
      const text = view.container.textContent ?? "";
      expect(text).toMatch(/streaming/i);
      expect(text).not.toMatch(/\d\s*%/);
      expect(text).not.toMatch(whole(57));
    }
  });
});
