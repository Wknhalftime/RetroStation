import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { useApplyMergeList, useMergeList, type SeenMergeGroup } from "@/api/mergeList";
import type {
  ApplyMergeListResult,
  MergeChange,
  MergeGroup,
  MergeList as List,
} from "@/lib/schemas/mergeList";

// Work merges phase 3b PR 4, SPEC S2, D6, D15, D27 (C57, C58, C74): every planned group with its
// works, flags and the air-play changes a pin can't hold, each applied by its own tick; Lance
// ticks only the groups to skip and presses Apply once.

const JOINS: Record<string, string> = {
  work_link: "Same MusicBrainz work",
  shared_artist: "Same artist",
  shared_name: "Same name",
};

const REFERENCES: Record<string, string> = {
  recording: "recording",
  match: "match",
  work_match: "match on the work",
  format_override: "format override",
  version_entry: "version entry",
  stored_performance: "stored performance",
};

function fileName(path: string | null): string {
  if (!path) return "No file";
  return path.split(/[\\/]/).pop() || path;
}

const changeKey = (group: MergeGroup, c: MergeChange) =>
  `${group.target_id}|${c.station_id}|${c.identity_id}|${c.to_file_id ?? ""}`;

function targetTitle(group: MergeGroup): string {
  return group.works.find((w) => w.work_id === group.target_id)?.title ?? group.target_id;
}

function summary(result: ApplyMergeListResult, list: List | undefined): string {
  const titles = new Map((list?.groups ?? []).map((g) => [g.target_id, targetTitle(g)]));
  const counts =
    `${result.merged} merged, ${result.waiting} waiting, ${result.failed} failed, ` +
    `${result.already_merged} already merged, ${result.changed} changed`;
  const notes = result.results
    .filter((r) => r.outcome !== "merged")
    .map((r) => {
      const title = titles.get(r.target_id) ?? r.target_id;
      return r.error ? `${title}: ${r.outcome}: ${r.error}` : `${title}: ${r.outcome}`;
    });
  return [counts, ...notes].join(". ");
}

function Group({
  group,
  skipped,
  ticked,
  disabled,
  onSkip,
  onTick,
}: {
  group: MergeGroup;
  skipped: boolean;
  ticked: ReadonlySet<string>;
  disabled: boolean;
  onSkip: () => void;
  onTick: (key: string) => void;
}) {
  const title = targetTitle(group);
  return (
    <section
      aria-label={`Merge into ${title}`}
      className="mb-4 rounded-xl border border-gray-200 bg-white p-4 shadow-sm"
    >
      <div className="mb-2 flex items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            aria-label={`Skip ${title}`}
            checked={skipped}
            disabled={disabled}
            onChange={onSkip}
          />
          Skip
        </label>
        <span className="text-sm text-gray-500">
          {group.joins.map((j) => JOINS[j] ?? j).join(", ")} · {group.plays.toLocaleString()} plays
        </span>
      </div>
      <ul className="mb-2 text-sm">
        {group.works.map((w) => (
          <li key={w.work_id}>
            {w.title} — {w.artists.join(", ") || "no credit"} · {w.plays.toLocaleString()} plays
            {w.work_id === group.target_id && (
              <span className="ml-2 rounded-full bg-indigo-100 px-2 py-0.5 text-xs text-indigo-800">
                Kept
              </span>
            )}
          </li>
        ))}
      </ul>
      {group.flags.length > 0 && (
        <ul className="mb-2 text-sm text-amber-800">
          {group.flags.map((f, n) => (
            <li key={`${f.kind}-${n}`}>{f.text}</li>
          ))}
        </ul>
      )}
      {group.failed && (
        <p className="mb-2 text-sm text-red-700">Last attempt failed: {group.failed.error}</p>
      )}
      {group.changes.length > 0 && (
        <table aria-label={`Changes to apply for ${title}`} className="w-full text-sm">
          <thead>
            <tr>
              <th>Apply</th>
              <th>Station</th>
              <th>Line</th>
              <th>Plays</th>
              <th>Plays now</th>
              <th>Would play</th>
            </tr>
          </thead>
          <tbody>
            {group.changes.map((c) => {
              const key = changeKey(group, c);
              return (
                <tr key={key}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Apply ${c.station}: ${c.title} (${c.artist})`}
                      checked={ticked.has(key)}
                      disabled={disabled}
                      onChange={() => onTick(key)}
                    />
                  </td>
                  <td>{c.station}</td>
                  <td>
                    {c.title} ({c.artist})
                  </td>
                  <td>{c.plays.toLocaleString()}</td>
                  <td title={c.from_path ?? undefined}>{fileName(c.from_path)}</td>
                  <td title={c.to_path ?? undefined}>{fileName(c.to_path)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function MergeList() {
  const { data, isLoading, isError, error: loadError } = useMergeList();
  const apply = useApplyMergeList();
  const [skipped, setSkipped] = useState<ReadonlySet<string>>(new Set());
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }
  if (isError || !data) {
    const said = loadError && "status" in loadError && loadError.status === 409;
    return (
      <p role="alert" className="rounded-md bg-red-50 p-4 text-sm text-red-700">
        {said ? loadError.message : "Failed to load the merge list. Please try again."}
      </p>
    );
  }

  const toggle = (set: (f: (s: ReadonlySet<string>) => ReadonlySet<string>) => void, key: string) =>
    set((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const applyAll = () => {
    if (apply.isPending) return;
    const groups: SeenMergeGroup[] = data.groups
      .filter((g) => !skipped.has(g.target_id))
      .map((g) => ({
        target_id: g.target_id,
        source_ids: g.source_ids,
        approvals: g.changes
          .filter((c) => ticked.has(changeKey(g, c)))
          .map((c) => ({
            station_id: c.station_id,
            identity_id: c.identity_id,
            file_id: c.to_file_id,
          })),
      }));
    apply.mutate(groups, {
      onSuccess: () => {
        setSkipped(new Set());
        setTicked(new Set());
      },
    });
  };

  return (
    <div>
      <PageHeader
        title="Merge list"
        description="Songs filed more than once. Tick only the groups to skip and each change you accept, then press Apply once."
      />
      {apply.error && (
        <p role="alert" className="mb-4 rounded-md bg-red-50 p-3 text-sm text-red-700">
          {apply.error.message}
        </p>
      )}
      {apply.data && (
        <p role="status" className="mb-4 rounded-md bg-green-50 p-3 text-sm text-green-800">
          {summary(apply.data, data)}
        </p>
      )}
      {data.groups.length === 0 ? (
        <p className="mb-6 text-sm text-gray-400">Nothing to merge.</p>
      ) : (
        <>
          <div className="mb-3 flex items-center gap-3">
            <button type="button" disabled={apply.isPending} onClick={applyAll}>
              Apply
            </button>
            <span className="text-sm text-gray-500">
              {(data.groups.length - skipped.size).toLocaleString()} to merge,{" "}
              {skipped.size.toLocaleString()} to skip
            </span>
          </div>
          {data.groups.map((g) => (
            <Group
              key={g.target_id}
              group={g}
              skipped={skipped.has(g.target_id)}
              ticked={ticked}
              disabled={apply.isPending}
              onSkip={() => toggle(setSkipped, g.target_id)}
              onTick={(key) => toggle(setTicked, key)}
            />
          ))}
        </>
      )}
      {data.duplicates.length > 0 && (
        <section aria-label="Recordings under two works" className="mb-6">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">
            Recordings under two works
          </h2>
          <ul className="text-sm">
            {data.duplicates.map((d) => (
              <li key={d.recording_mbid}>
                {d.recording_mbid}: {d.works.map((w) => w.title).join(", ")}
              </li>
            ))}
          </ul>
        </section>
      )}
      {data.kept_works.length > 0 && (
        <section aria-label="Empty works kept">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">
            Empty works kept
          </h2>
          <ul className="text-sm">
            {data.kept_works.map((k) => (
              <li key={k.work_id}>
                {k.title}: {k.references.map((r) => REFERENCES[r] ?? r).join(", ")}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
