// @vitest-environment jsdom
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from "@/api/client";
import { AssociatedWorks } from "./AssociatedWorks";

// Duplicate works (the same song twice under one artist) are folded into the
// work whose page is open, through POST /works/{target}/merge.

const mockedApiFetch = vi.mocked(apiFetch);
const KEEP = "w-keep";
const WORK_URL = `/api/v1/library/works/${KEEP}`;
const MERGE_URL = `/api/v1/library/works/${KEEP}/merge`;

const WORK = {
  id: KEEP,
  title: "I Ran (So Far Away)",
  artist_id: "a-fos",
  recordings: [],
  song_master: null,
  format_overrides: [],
};

const summary = (id: string, title: string, recording_count = 1) => ({
  id,
  title,
  recording_count,
  has_master: false,
  version_types: [],
});

const ARTIST = {
  id: "a-fos",
  name: "A Flock of Seagulls",
  sort_name: "Flock of Seagulls, A",
  disambiguation: null,
  mbid: null,
  origin: "local",
  works: [
    summary(KEEP, "I Ran (So Far Away)", 2),
    summary("w-iran", "I Ran", 2),
    summary("w-space", "Space Age Love Song"),
    summary("w-wish", "Wishing (If I Had a Photograph of You)"),
  ],
};

interface Serve {
  artist?: typeof ARTIST;
  merge?: () => Promise<unknown>;
}

function serve({ artist = ARTIST, merge }: Serve = {}) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (url === MERGE_URL && options?.method === "POST") {
      return merge
        ? merge()
        : Promise.resolve({
            merged_file_count: 2,
            deleted_work_count: 1,
            dropped_override_count: 0,
          });
    }
    if (url === WORK_URL) return Promise.resolve(WORK);
    return Promise.resolve(artist);
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/library/artists/a-fos/works/${KEEP}`]}>
        <Routes>
          <Route path="/library/artists/:artist_id/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function openDialog() {
  fireEvent.click(await screen.findByRole("button", { name: /Merge duplicates/ }));
  return screen.getByRole("dialog");
}

function mergeCalls() {
  return mockedApiFetch.mock.calls.filter(([url]) => url === MERGE_URL);
}

beforeAll(() => {
  // jsdom has no modal dialogs; open the element the way the browser would.
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute("open");
  };
});

describe("AssociatedWorks, merging duplicate works into the open one", () => {
  beforeEach(() => mockedApiFetch.mockReset());

  it("lists the artist's other works, not the open one", async () => {
    serve();
    renderPage();

    const dialog = await openDialog();

    const boxes = within(dialog).getAllByRole("checkbox");
    expect(boxes.map((b) => b.getAttribute("aria-label"))).toEqual([
      "I Ran",
      "Space Age Love Song",
      "Wishing (If I Had a Photograph of You)",
    ]);
  });

  it("narrows the list by title", async () => {
    serve();
    renderPage();

    const dialog = await openDialog();
    fireEvent.change(within(dialog).getByRole("searchbox"), { target: { value: "ran" } });

    expect(
      within(dialog)
        .getAllByRole("checkbox")
        .map((b) => b.getAttribute("aria-label"))
    ).toEqual(["I Ran"]);
  });

  it("will not merge until a work is picked", async () => {
    serve();
    renderPage();

    const dialog = await openDialog();

    const merge = within(dialog).getByRole("button", { name: /^Merge/ });
    expect((merge as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(merge);
    expect(mergeCalls()).toHaveLength(0);
  });

  it("warns that the picked works are deleted and the open one is kept", async () => {
    serve();
    renderPage();

    const dialog = await openDialog();

    expect(dialog.textContent).toContain("I Ran (So Far Away)");
    expect(dialog.textContent).toMatch(/deleted/i);
  });

  it("sends every picked work as a source of the open one", async () => {
    serve();
    renderPage();

    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "I Ran" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Space Age Love Song" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Merge 2 works into this one" }));

    await screen.findByRole("status");
    expect(mergeCalls()).toHaveLength(1);
    const [, options] = mergeCalls()[0];
    expect(options?.method).toBe("POST");
    expect(JSON.parse(String(options?.body))).toEqual({
      source_work_ids: ["w-iran", "w-space"],
    });
  });

  it("keeps a pick hidden by the filter", async () => {
    serve();
    renderPage();

    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Space Age Love Song" }));
    fireEvent.change(within(dialog).getByRole("searchbox"), { target: { value: "ran" } });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "I Ran" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Merge 2 works into this one" }));

    await screen.findByRole("status");
    const [, options] = mergeCalls()[0];
    expect(JSON.parse(String(options?.body)).source_work_ids).toEqual(["w-space", "w-iran"]);
  });

  it("says what was merged and closes the dialog", async () => {
    serve();
    renderPage();

    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "I Ran" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Merge 1 work into this one" }));

    expect((await screen.findByRole("status")).textContent).toContain(
      "Merged 1 work into this one: 2 files moved."
    );
    expect(dialog.hasAttribute("open")).toBe(false);
  });

  it("shows why the server refused, and stays open", async () => {
    serve({ merge: () => Promise.reject(new ApiError(404, `Work ${KEEP} not found`)) });
    renderPage();

    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "I Ran" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Merge 1 work into this one" }));

    expect((await within(dialog).findByRole("alert")).textContent).toContain(
      `Work ${KEEP} not found`
    );
    expect(dialog.hasAttribute("open")).toBe(true);
  });

  it("says so when the artist has no other works", async () => {
    serve({ artist: { ...ARTIST, works: [summary(KEEP, "I Ran (So Far Away)", 2)] } });
    renderPage();

    const dialog = await openDialog();

    expect(within(dialog).queryAllByRole("checkbox")).toHaveLength(0);
    expect(dialog.textContent).toContain("no other works");
  });
});
