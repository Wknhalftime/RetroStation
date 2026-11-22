import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import {
  MERGE_LIST_PAGE,
  useApplyMergeList,
  useKeepApart,
  useMergeList,
  useUndoKeepApart,
  type SeenMergeGroup,
} from "@/api/mergeList";
import type {
  ApplyMergeListResult,
  KeptApart,
  MergeChange,
  MergeGroup,
  MergeList as List,
} from "@/lib/schemas/mergeList";

// Work merges phase 3b PR 4, SPEC S2, D6, D15, D27 (C57, C58, C74): every planned group with its
// works, flags and the air-play changes a pin can't hold, each applied by its own tick; Lance
// ticks only the groups to skip and presses Apply once. PR 5c (D40, D55, D56): each group's
// "Keep apart" keeps every pair of its works apart; a held group shows why, has no Skip tick and
// is never applied; "Kept apart" lists every pair, each with Undo.

const JOINS: Record<string, string> = {
  work_link: "Same MusicBrainz work",
  shared_artist: "Same artist",
  shared_name: "Same name",
};

const REFERENCES: Record<string, string> = {
  recording: "recording",
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

function names(titles: string[]): string {
  if (titles.length <= 1) return titles.join("");
  return `${titles.slice(0, -1).join(", ")} and ${titles[titles.length - 1]}`;
}

// D56: "Not merged: A and B are kept apart; C joins them." Each pair in the group's own order.
function heldNotice(group: MergeGroup): string {
  const order = group.works.map((w) => w.work_id);
  const title = (id: string) => group.works.find((w) => w.work_id === id)?.title ?? id;
  const kept = new Set(group.held.flatMap((p) => [p.first_work_id, p.second_work_id]));
  const pairs = group.held.map((p) => {
    const [a, b] = [p.first_work_id, p.second_work_id].sort(
      (x, y) => order.indexOf(x) - order.indexOf(y)
    );
    return `${title(a)} and ${title(b)}`;
  });
  const joiners = order.filter((id) => !kept.has(id)).map(title);
  const joining =
    joiners.length === 0
      ? ""
      : `; ${names(joiners)} ${joiners.length === 1 ? "joins" : "join"} them`;
  return `Not merged: ${pairs.join("; ")} are kept apart${joining}.`;
}

function KeptWork({ work }: { work: KeptApart["first"] }) {
  return (
    <>
      <Link to={`/library/works/${work.work_id}`} className="text-indigo-700 hover:underline">
        {work.title}
      </Link>{" "}
      ({work.artists.join(", ") || "No credited artist"})
    </>
  );
}

// Q9, Q3, Q6: every kept pair as the page sends it, each with Undo.
function KeptApartSection({
  pairs,
  disabled,
  onUndo,
}: {
  pairs: KeptApart[];
  disabled: boolean;
  onUndo: (pair: KeptApart) => void;
}) {
  if (pairs.length === 0) return null;
  return (
    <section aria-label="Kept apart" className="mt-6">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">
        Kept apart
      </h2>
      <ul className="text-sm">
        {pairs.map((k) => (
          <li key={`${k.first.work_id}|${k.second.work_id}`} className="mb-1">
            <KeptWork work={k.first} /> and <KeptWork work={k.second} /> ·{" "}
            <time dateTime={k.kept_at}>{new Date(k.kept_at).toLocaleDateString()}</time>
            {k.note && <> · {k.note}</>}
            {k.musicbrainz_links && (
              <span className="ml-2 text-amber-800">MusicBrainz links these works</span>
            )}{" "}
            <button
              type="button"
              aria-label={`Undo: ${k.first.title} and ${k.second.title}`}
              disabled={disabled}
              onClick={() => onUndo(k)}
            >
              Undo
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Group({
  group,
  skipped,
  ticked,
  disabled,
  keeping,
  onSkip,
  onTick,
  onKeepApart,
}: {
  group: MergeGroup;
  skipped: boolean;
  ticked: ReadonlySet<string>;
  disabled: boolean;
  keeping: boolean;
  onSkip: () => void;
  onTick: (key: string) => void;
  onKeepApart: () => void;
}) {
  const title = targetTitle(group);
  const held = group.held.length > 0;
  return (
    <section
      aria-label={`Merge into ${title}`}
      className="mb-4 rounded-xl border border-gray-200 bg-white p-4 shadow-sm"
    >
      {held && (
        <p role="note" className="mb-2 rounded-md bg-amber-50 p-2 text-sm text-amber-900">
          {heldNotice(group)}
        </p>
      )}
      <div className="mb-2 flex items-center gap-3">
        {!held && (
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
        )}
        <button
          type="button"
          aria-label={`Keep apart: ${title}`}
          disabled={disabled || keeping}
          onClick={onKeepApart}
        >
          Keep apart
        </button>
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

function lastPageOffset(total: number): number {
  return Math.max(0, Math.floor((total - 1) / MERGE_LIST_PAGE) * MERGE_LIST_PAGE);
}

export function MergeList() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading, isError, error: loadError } = useMergeList(offset);
  const apply = useApplyMergeList();
  const keepApart = useKeepApart();
  const undo = useUndoKeepApart();
  const [skipped, setSkipped] = useState<ReadonlySet<string>>(new Set());
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());

  // D37: a page an Apply emptied steps back to the last page that has groups.
  const emptied = data !== undefined && data.groups.length === 0 && data.offset > 0;
  const stepBackTo = emptied ? lastPageOffset(data.total) : null;
  useEffect(() => {
    if (stepBackTo !== null) setOffset(stepBackTo);
  }, [stepBackTo]);

  const goTo = (next: number) => {
    setSkipped(new Set());
    setTicked(new Set());
    setOffset(next);
  };

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
  // D56: a held group is never applied, nor counted to merge.
  const mergeable = data.groups.filter((g) => g.held.length === 0);
  const skippedShown = mergeable.filter((g) => skipped.has(g.target_id)).length;
  const applyAll = () => {
    if (apply.isPending) return;
    const groups: SeenMergeGroup[] = mergeable
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
      {keepApart.error && (
        <p role="alert" className="mb-4 rounded-md bg-red-50 p-3 text-sm text-red-700">
          Keep apart failed: {keepApart.error.message}
        </p>
      )}
      {undo.error && (
        <p role="alert" className="mb-4 rounded-md bg-red-50 p-3 text-sm text-red-700">
          Undo failed: {undo.error.message}
        </p>
      )}
      {apply.data && (
        <p role="status" className="mb-4 rounded-md bg-green-50 p-3 text-sm text-green-800">
          {summary(apply.data, data)}
        </p>
      )}
      {data.total === 0 ? (
        <p className="mb-6 text-sm text-gray-400">Nothing to merge.</p>
      ) : data.groups.length === 0 ? (
        <p className="mb-6 text-sm text-gray-400">Nothing left on this page.</p>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <button type="button" disabled={apply.isPending} onClick={applyAll}>
              Apply
            </button>
            <button
              type="button"
              disabled={apply.isPending || data.offset === 0}
              onClick={() => goTo(Math.max(0, data.offset - MERGE_LIST_PAGE))}
            >
              Previous
            </button>
            <button
              type="button"
              disabled={apply.isPending || data.offset + data.limit >= data.total}
              onClick={() => goTo(data.offset + MERGE_LIST_PAGE)}
            >
              Next
            </button>
            <span className="text-sm text-gray-500">
              {`Groups ${(data.offset + 1).toLocaleString()}–${(
                data.offset + data.groups.length
              ).toLocaleString()} of ${data.total.toLocaleString()}`}
            </span>
            <span className="text-sm text-gray-500">
              {(mergeable.length - skippedShown).toLocaleString()} to merge,{" "}
              {skippedShown.toLocaleString()} to skip
            </span>
          </div>
          {data.groups.map((g) => (
            <Group
              key={g.target_id}
              group={g}
              skipped={skipped.has(g.target_id)}
              ticked={ticked}
              disabled={apply.isPending}
              keeping={keepApart.isPending}
              onSkip={() => toggle(setSkipped, g.target_id)}
              onTick={(key) => toggle(setTicked, key)}
              onKeepApart={() => keepApart.mutate([g.target_id, ...g.source_ids])}
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
      <KeptApartSection
        pairs={data.kept_apart}
        disabled={undo.isPending}
        onUndo={(k) =>
          undo.mutate({ first_work_id: k.first.work_id, second_work_id: k.second.work_id })
        }
      />
    </div>
  );
}
