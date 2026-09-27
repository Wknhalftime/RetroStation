// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { WorkFilesTable } from "./WorkFilesTable";
import type { RecordingDetail } from "@/lib/schemas/works";

const file = (id: string, name: string, file_status: "present" | "missing") => ({
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
});
