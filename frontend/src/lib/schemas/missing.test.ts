import { describe, it, expect } from "vitest";
import { MissingFilePageSchema } from "./missing";

const page = {
  items: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      file_path: "D:\\Music\\old\\kiss.flac",
      artist_name: "Prince",
      track_title: "Kiss",
      release_title: null,
      missing_since: "2026-09-27T10:00:00Z",
      work_id: "w1",
      work_title: "Kiss",
      match_count: 2,
      work_has_present_file: true,
      candidates: [{ id: "33333333-3333-4333-8333-333333333333", file_path: "D:\\new.flac" }],
    },
  ],
  total: 1,
  total_match_count: 2,
};

describe("MissingFilePageSchema", () => {
  it("parses the API's page", () => {
    expect(MissingFilePageSchema.parse(page).items[0].candidates).toHaveLength(1);
  });

  it("rejects a negative match count", () => {
    const bad = { ...page, items: [{ ...page.items[0], match_count: -1 }] };
    expect(() => MissingFilePageSchema.parse(bad)).toThrow();
  });
});
