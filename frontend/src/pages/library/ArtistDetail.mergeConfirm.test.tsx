// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ArtistDetail } from "./ArtistDetail";

// Phase 3b PR 3 (SPEC D3, Q7, Q8; design C58, C74): "Merge N works" asks before air play
// changes. POST merge either merges (200, as today) or, when the merge would change what a
// line plays where a pin can't hold it, answers 409 with the changes. The page lists them
// (station, the line as logged, its plays there, the file it plays now and would play) and
// asks; "Merge anyway" sends POST merge/approved with each change's (station, line, new file)
// as an approval. A confirm that finds other changes answers 409 again, and the page asks
// again. These tests mock fetch, so the 409 goes through the real API client.

const ARTIST_URL = "/api/v1/library/artists/a-fos";
const MERGE_URL = "/api/v1/library/works/w-iran-full/merge";
const APPROVED_URL = "/api/v1/library/works/w-iran-full/merge/approved";

const summary = (id: string, title: string, recording_count = 1) => ({
  id,
  title,
  recording_count,
  has_master: true,
  version_types: [],
});

const ARTIST = {
  id: "a-fos",
  name: "A Flock of Seagulls",
  sort_name: "Flock of Seagulls, A",
  disambiguation: null,
  mbid: null,
  origin: "local",
  merged_into: null,
  members: [],
  works: [summary("w-iran", "I Ran", 1), summary("w-iran-full", "I Ran (So Far Away)", 2)],
};

const change = (identity: string, title: string, plays: number, to: string) => ({
  station_id: "s-kaaa",
  station: "KAAA",
  identity_id: identity,
  title,
  artist: "A FLOCK OF SEAGULLS",
  plays,
  from_file_id: "f-album",
  from_path: "D:\\Music\\i-ran-album.flac",
  to_file_id: to,
  to_path: `D:\\Music\\${to}.mp3`,
});

const AC_EDIT = change("i-ran", "I RAN", 12, "i-ran-ac-edit");
const LATER = change("i-ran-2", "I RAN (SO FAR AWAY)", 3, "i-ran-ac-mix");
const MERGED = { merged_file_count: 1, deleted_work_count: 1, dropped_override_count: 0 };

type Reply = { status: number; body: unknown };
const refusal = (...changes: unknown[]): Reply => ({
  status: 409,
  body: { detail: { message: "This merge changes what some lines play.", changes } },
});
const ok: Reply = { status: 200, body: MERGED };

const posts: { url: string; body: unknown }[] = [];

/** Answer GET artist with ARTIST, each POST with the next of *replies*, anything else 404. */
function serve(...replies: Reply[]) {
  const queue = [...replies];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input).replace(/^https?:\/\/[^/]+/, "");
    const json = (status: number, body: unknown) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
    if (init?.method === "POST") {
      posts.push({ url, body: JSON.parse(String(init.body)) });
      const reply = queue.shift() ?? { status: 500, body: { detail: "no reply queued" } };
      return json(reply.status, reply.body);
    }
    if (url === ARTIST_URL) return json(200, ARTIST);
    return json(404, { detail: `unexpected ${url}` });
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/artists/a-fos"]}>
        <Routes>
          <Route path="/library/artists/:artist_id" element={<ArtistDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function confirmMerge() {
  fireEvent.click(await screen.findByRole("checkbox", { name: "Select I Ran" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Select I Ran (So Far Away)" }));
  fireEvent.click(screen.getByRole("button", { name: "Merge 2 works" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm merge" }));
}

const changeList = () => screen.findByRole("list", { name: "What this merge changes" });

describe("ArtistDetail, a merge that changes what plays", () => {
  beforeEach(() => {
    posts.length = 0;
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("merges at once when nothing that plays would change", async () => {
    serve(ok);
    renderPage();

    await confirmMerge();

    expect((await screen.findByRole("status")).textContent).toContain("Merged 1 work");
    expect(posts.map((p) => p.url)).toEqual([MERGE_URL]);
    expect(screen.queryByRole("list", { name: "What this merge changes" })).toBeNull();
  });

  it("lists each change the server names and asks before merging", async () => {
    serve(refusal(AC_EDIT));
    renderPage();

    await confirmMerge();

    const items = within(await changeList()).getAllByRole("listitem");
    expect(items).toHaveLength(1);
    const text = items[0].textContent ?? "";
    for (const shown of ["KAAA", "I RAN", "A FLOCK OF SEAGULLS", "12", "i-ran-album.flac"]) {
      expect(text).toContain(shown);
    }
    expect(text).toContain("i-ran-ac-edit.mp3");
    expect(screen.getByRole("button", { name: "Merge anyway" })).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    expect(posts.map((p) => p.url)).toEqual([MERGE_URL]);
  });

  it("sends the approvals once confirmed", async () => {
    serve(refusal(AC_EDIT), ok);
    renderPage();
    await confirmMerge();
    await changeList();

    fireEvent.click(screen.getByRole("button", { name: "Merge anyway" }));

    expect((await screen.findByRole("status")).textContent).toContain("Merged 1 work");
    expect(posts.map((p) => p.url)).toEqual([MERGE_URL, APPROVED_URL]);
    expect(posts[1].body).toEqual({
      source_work_ids: ["w-iran"],
      approvals: [{ station_id: "s-kaaa", identity_id: "i-ran", file_id: "i-ran-ac-edit" }],
    });
    expect(screen.queryByRole("list", { name: "What this merge changes" })).toBeNull();
  });

  it("sends nothing more when cancelled", async () => {
    serve(refusal(AC_EDIT));
    renderPage();
    await confirmMerge();
    await changeList();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(posts).toHaveLength(1);
    expect(screen.queryByRole("list", { name: "What this merge changes" })).toBeNull();
    expect(screen.getByRole("button", { name: "Merge 2 works" })).toBeTruthy();
  });

  it("asks again when the confirm finds other changes", async () => {
    serve(refusal(AC_EDIT), refusal(AC_EDIT, LATER), ok);
    renderPage();
    await confirmMerge();
    await changeList();

    fireEvent.click(screen.getByRole("button", { name: "Merge anyway" }));

    expect(await within(await changeList()).findAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Merge anyway" }));

    await screen.findByRole("status");
    expect(posts.map((p) => p.url)).toEqual([MERGE_URL, APPROVED_URL, APPROVED_URL]);
    expect(posts[2].body).toEqual({
      source_work_ids: ["w-iran"],
      approvals: [
        { station_id: "s-kaaa", identity_id: "i-ran", file_id: "i-ran-ac-edit" },
        { station_id: "s-kaaa", identity_id: "i-ran-2", file_id: "i-ran-ac-mix" },
      ],
    });
  });

  it("shows any other refusal as before", async () => {
    serve({ status: 404, body: { detail: "Work w-iran-full not found" } });
    renderPage();

    await confirmMerge();

    expect((await screen.findByRole("alert")).textContent).toContain("Work w-iran-full not found");
    expect(screen.queryByRole("list", { name: "What this merge changes" })).toBeNull();
  });
});
