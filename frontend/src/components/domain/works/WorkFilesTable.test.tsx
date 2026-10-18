// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { WorkFilesTable } from "./WorkFilesTable";
import type { FileInfo, RecordingDetail } from "@/lib/schemas/works";

const file = (id: string, name: string, file_status: FileInfo["file_status"]) => ({
  id,
  file_path: `D:\\Music\\${name}`,
  format: "flac",
  bitrate: 488,
  duration_ms: 54_040,
  track_title: "Ezekiel 25:17",
  release_title: "Pulp Fiction",
  enrichment_status: "enriched",
  file_status,
});

const recordings: RecordingDetail[] = [
  {
    id: "rec-1",
    title: "Ezekiel 25:17",
    version_type: "original",
    duration_ms: 52_000,
    files: [
      file("11111111-1111-4111-8111-111111111111", "old.flac", "missing"),
      file("22222222-2222-4222-8222-222222222222", "new.flac", "present"),
    ],
  },
];

describe("WorkFilesTable", () => {
  it("badges a missing file and offers no master action on it", () => {
    render(
      <WorkFilesTable
        recordings={recordings}
        masterFileId={null}
        masterMethod={null}
        onSetMaster={vi.fn()}
      />
    );

    expect(screen.getAllByText("Missing")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Set as master file" })).toHaveLength(1);
  });

  it("treats a deleted file as not on disk", () => {
    const withDeleted: RecordingDetail[] = [
      {
        ...recordings[0],
        files: [
          file("33333333-3333-4333-8333-333333333333", "gone.flac", "deleted"),
          file("22222222-2222-4222-8222-222222222222", "new.flac", "present"),
        ],
      },
    ];
    render(
      <WorkFilesTable
        recordings={withDeleted}
        masterFileId={null}
        masterMethod={null}
        onSetMaster={vi.fn()}
      />
    );

    expect(screen.getAllByText("Missing")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Set as master file" })).toHaveLength(1);
  });

  it("names the album each file is from", () => {
    const fromTwoAlbums: RecordingDetail[] = [
      {
        ...recordings[0],
        files: [
          file("22222222-2222-4222-8222-222222222222", "new.flac", "present"),
          {
            ...file("44444444-4444-4444-8444-444444444444", "hits.flac", "present"),
            release_title: "Greatest Hits",
          },
        ],
      },
    ];
    render(
      <WorkFilesTable
        recordings={fromTwoAlbums}
        masterFileId={null}
        masterMethod={null}
        onSetMaster={vi.fn()}
      />
    );

    expect(screen.getByText("Pulp Fiction")).toBeTruthy();
    expect(screen.getByText("Greatest Hits")).toBeTruthy();
  });

  it("shows no album line for a file without an album tag", () => {
    const untagged: RecordingDetail[] = [
      {
        ...recordings[0],
        files: [
          {
            ...file("22222222-2222-4222-8222-222222222222", "new.flac", "present"),
            release_title: null,
          },
        ],
      },
    ];
    render(
      <WorkFilesTable
        recordings={untagged}
        masterFileId={null}
        masterMethod={null}
        onSetMaster={vi.fn()}
      />
    );

    expect(screen.queryByTestId("file-album")).toBeNull();
  });
});

// Cue editor PR 3, Task 11 (spec "How it's reached"): each file row that is present on disk
// gets a Cues link to the editor page, /library/files/<id>/cues. A Link needs a router, so these
// cases render inside one.
describe("WorkFilesTable: Cues link", () => {
  const MISSING_ID = "11111111-1111-4111-8111-111111111111";
  const PRESENT_ID = "22222222-2222-4222-8222-222222222222";
  const OTHER_PRESENT_ID = "44444444-4444-4444-8444-444444444444";

  function renderTable(rows: RecordingDetail[]) {
    return render(
      <MemoryRouter>
        <WorkFilesTable
          recordings={rows}
          masterFileId={null}
          masterMethod={null}
          onSetMaster={vi.fn()}
        />
      </MemoryRouter>
    );
  }

  it("a present file row links to /library/files/<id>/cues", () => {
    renderTable(recordings);

    const links = screen.getAllByRole("link", { name: "Cues" });
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe(`/library/files/${PRESENT_ID}/cues`);
  });

  it("a missing file row has no Cues link", () => {
    const { container } = renderTable(recordings);

    expect(container.querySelector(`a[href*="${MISSING_ID}"]`)).toBeNull();
    expect(screen.getAllByRole("link", { name: "Cues" })).toHaveLength(1);
  });

  it("a deleted file row has no Cues link either", () => {
    const { container } = renderTable([
      {
        ...recordings[0],
        files: [file(MISSING_ID, "gone.flac", "deleted"), file(PRESENT_ID, "new.flac", "present")],
      },
    ]);

    expect(container.querySelector(`a[href*="${MISSING_ID}"]`)).toBeNull();
    const links = screen.getAllByRole("link", { name: "Cues" });
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe(`/library/files/${PRESENT_ID}/cues`);
  });

  it("every present file gets its own link, whichever recording it is under", () => {
    renderTable([
      recordings[0],
      {
        ...recordings[0],
        id: "rec-2",
        title: "Ezekiel 25:17 (live)",
        files: [file(OTHER_PRESENT_ID, "live.flac", "present")],
      },
    ]);

    const hrefs = screen
      .getAllByRole("link", { name: "Cues" })
      .map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual([
      `/library/files/${PRESENT_ID}/cues`,
      `/library/files/${OTHER_PRESENT_ID}/cues`,
    ]);
  });
});
