// PR G2, Task 9 (traceability VA3; rows D77, D89, D90, D91, D95, PG7): the Streaming page's
// contracts for its meters. GET /api/v1/streaming/cue-coverage answers {analysable, ready,
// failed, unhashed} (the plan's route table). The cost meter is not a route (D90): it is the
// progress_data of the one "stream_resources" progress row (design note 15).
import { describe, it, expect } from "vitest";
import { CueCoverageSchema, ResourceMeterSchema } from "./streamingMeters";

const coverage = { analysable: 500, ready: 120, failed: 5, unhashed: 7 };

const meterRow = {
  open_streams: 2,
  scope: "audio_engines",
  streams: [
    { cpu_percent: 13.1, memory_mb: 74.2, warming: false },
    { cpu_percent: null, memory_mb: 70.0, warming: true },
  ],
  cost: { cpu_percent: 13.4, memory_mb: 74.6, source: "measured" },
  machine: {
    threads: 16,
    memory_total_mb: 65430,
    memory_available_mb: 40100,
    cpu_percent: null,
  },
  suggested_max: 57,
  limited_by: "processor",
  budget: { cpu_share: 0.5, memory_share: 0.25 },
};

describe("streaming meter schemas", () => {
  it("VA3: parses the coverage and the meter row; rejects negative counts", () => {
    expect(CueCoverageSchema.parse(coverage)).toEqual(coverage);
    const meter = ResourceMeterSchema.parse(meterRow);
    expect(meter.suggested_max).toBe(57);
    expect(meter.streams[1]?.cpu_percent).toBeNull();
    expect(meter.cost.source).toBe("measured");
    expect(ResourceMeterSchema.parse({ ...meterRow, limited_by: "memory" }).limited_by).toBe(
      "memory"
    );

    expect(CueCoverageSchema.safeParse({ ...coverage, ready: -1 }).success).toBe(false);
    expect(CueCoverageSchema.safeParse({ ...coverage, unhashed: -3 }).success).toBe(false);
    expect(ResourceMeterSchema.safeParse({ ...meterRow, open_streams: -1 }).success).toBe(false);
    expect(ResourceMeterSchema.safeParse({ ...meterRow, suggested_max: 0 }).success).toBe(false);
    expect(
      ResourceMeterSchema.safeParse({
        ...meterRow,
        cost: { ...meterRow.cost, source: "guessed" },
      }).success
    ).toBe(false);
  });
});
