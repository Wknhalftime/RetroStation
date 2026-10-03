import { useProgressStore } from "@/store/progressStore";
import {
  ResourceMeterSchema,
  type ResourceMeter as ResourceMeterData,
} from "@/lib/schemas/streamingMeters";

// ---------------------------------------------------------------------------
// D90/D95: the cost meter travels in the one "stream_resources" progress row
// (through /ws into the progress store), never a route. D95: this section's
// title is pinned verbatim: "Audio engine cost per stream". It counts the
// Liquidsoap audio engines only, and never estimates the app's own share.
// ---------------------------------------------------------------------------

const TITLE = "Audio engine cost per stream";

function pct(value: number | null): string {
  if (value === null) return "warming up";
  return `${Math.round(value * 10) / 10}%`;
}

function mb(value: number): string {
  return `${Math.round(value).toLocaleString()} MB`;
}

function NoMeter() {
  return (
    <p className="text-sm text-gray-500">
      The meter runs while streaming is on; it shows nothing while streaming is off, or between a
      shutdown and the next start.
    </p>
  );
}

function MeterFigures({ meter }: { meter: ResourceMeterData }) {
  const { open_streams, streams, cost, machine, suggested_max, limited_by, budget } = meter;
  const limitedByLabel = limited_by === "processor" ? "processor" : "memory";
  const cpuSharePercent = Math.round(budget.cpu_share * 100);
  const memorySharePercent = Math.round(budget.memory_share * 100);

  return (
    <div className="space-y-3 text-sm text-gray-700">
      <p>
        {open_streams} open stream{open_streams === 1 ? "" : "s"}: about {pct(cost.cpu_percent)} CPU
        and {mb(cost.memory_mb)} memory per stream.
      </p>

      {cost.source === "reference" && (
        <p className="text-xs text-amber-600">
          These are the engine test&apos;s reference numbers — no stream has been measured live yet.
        </p>
      )}

      {streams.length > 0 && (
        <ul className="space-y-0.5 text-xs text-gray-500">
          {streams.map((stream, index) => (
            <li key={index}>
              Engine {index + 1}: {stream.warming ? "warming up" : pct(stream.cpu_percent)} CPU,{" "}
              {mb(stream.memory_mb)} memory.
            </li>
          ))}
        </ul>
      )}

      <p className="text-xs text-gray-500">
        Machine: {machine.threads} threads, {mb(machine.memory_total_mb)} total memory (
        {mb(machine.memory_available_mb)} available),{" "}
        {machine.cpu_percent === null
          ? "still measuring its own load."
          : `currently about ${pct(machine.cpu_percent)} CPU.`}
      </p>

      <p className="text-xs text-gray-500">
        Suggested limit: {suggested_max} listeners, limited by {limitedByLabel}. Disk is not
        counted.
      </p>

      <p className="text-xs text-gray-500">
        Budget: {cpuSharePercent}% of the processor threads, {memorySharePercent}% of the memory.
      </p>
    </div>
  );
}

export function ResourceMeter() {
  const pageTasks = useProgressStore((s) => s.pageTasks);
  const meterTask = pageTasks.find((t) => t.task_type === "stream_resources") ?? null;
  const parsed =
    meterTask?.status === "running" ? ResourceMeterSchema.safeParse(meterTask.progress_data) : null;

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-gray-700">{TITLE}</h3>
      {parsed?.success ? <MeterFigures meter={parsed.data} /> : <NoMeter />}
    </div>
  );
}
