// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { WorkFilesTable } from "./WorkFilesTable";
import type { RecordingDetail } from "@/lib/schemas/works";

// A work's page lists files by recording, so it also shows a file of another work that
// shares a recording (Stone Sour › "Bother" shows the album copy tagged "Corey Taylor").

const PROMO = "11111111-1111-4111-8111-111111111111";
const ALBUM = "22222222-2222-4222-8222-222222222222";

const file = (id: string, name: string) => ({
  id,
  file_path: `D:\\Music\\${name}`,
  format: "flac",
  bitrate: 900,
  duration_ms: 240_000,
  track_title: "Bother",
  release_title: null,
  enrichment_status: "enriched",
  file_status: "present" as const,
});

const recordings: RecordingDetail[] = [
  {
    id: "rec-shared",
    title: "Bother",
    version_type: "original",
    duration_ms: 240_000,
    files: [
      file(PROMO, "promo.flac"),
      {
        ...file(ALBUM, "album.flac"),
        other_work: { id: "w-ct", title: "Bother", artist_name: "Corey Taylor" },
      },
    ],
  },
];

function renderTable(onSetMaster = vi.fn()) {
  render(
    <WorkFilesTable
      recordings={recordings}
      masterFileId={null}
      masterMethod={null}
      onSetMaster={onSetMaster}
    />,
    { wrapper: MemoryRouter }
  );
  return onSetMaster;
}

describe("WorkFilesTable, a file shown from another work", () => {
  it("names the work the file is on", () => {
    renderTable();

    expect(screen.getAllByTestId("file-other-work")).toHaveLength(1);
    expect(screen.getByTestId("file-other-work").textContent).toBe("on Corey Taylor › Bother");
  });

  it("says its crown moves the file here as well as making it master", () => {
    renderTable();

    expect(screen.getAllByRole("button", { name: "Set as master file" })).toHaveLength(1);
    expect(
      screen.getAllByRole("button", { name: "Move into this song and set as master file" })
    ).toHaveLength(1);
  });

  it("crowns it like any other file", () => {
    const onSetMaster = renderTable();

    fireEvent.click(
      screen.getByRole("button", { name: "Move into this song and set as master file" })
    );

    expect(onSetMaster).toHaveBeenCalledWith(ALBUM);
  });
});
