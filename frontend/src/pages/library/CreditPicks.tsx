import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { useApplyCreditPicks, useCreditPicks, usePickVersionLine } from "@/api/creditPicks";
import type { CreditPick, CreditPickCandidate, CreditPickFile } from "@/lib/schemas/creditPicks";

// Performances phase 3a PR 3, SPEC S9 (C57, C64, C65; D7): the proposals Apply takes, each
// skipped only when ticked, and the version lines, each picked one at a time.

const nameOf = (p: CreditPick) => `${p.title} (${p.artist_name})`;

function fileName(file: CreditPickFile): string {
  return file.file_path.split(/[\\/]/).pop() || file.file_path;
}

function candidateLabel(candidate: CreditPickCandidate): string {
  const plays = candidate.would_play ? fileName(candidate.would_play) : "no file";
  return `${candidate.credit_names.join(" & ")} (${plays})`;
}

function FileCell({ file }: { file: CreditPickFile | null }) {
  if (!file) return <td className="text-gray-400">No file</td>;
  return <td title={file.file_path}>{fileName(file)}</td>;
}

export function CreditPicks() {
  const { data, isLoading, isError } = useCreditPicks();
  const apply = useApplyCreditPicks();
  const pick = usePickVersionLine();
  const [skipped, setSkipped] = useState<ReadonlySet<string>>(new Set());
  const error = apply.error ?? pick.error;

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }
  if (isError || !data) {
    return (
      <p role="alert" className="rounded-md bg-red-50 p-4 text-sm text-red-700">
        Failed to load credit picks. Please try again.
      </p>
    );
  }

  const credit = data.credit;
  const lines = data.version_lines;
  const skipIds = credit.filter((p) => skipped.has(p.id)).map((p) => p.id);
  const applyIds = credit.filter((p) => !skipped.has(p.id)).map((p) => p.id);
  const toggle = (id: string) =>
    setSkipped((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const applyAll = () => {
    if (apply.isPending) return;
    apply.mutate({ apply: applyIds, skip: skipIds }, { onSuccess: () => setSkipped(new Set()) });
  };
  const choose = (line: CreditPick, performanceId: string | null) =>
    pick.mutate({ id: line.id, performanceId });
  const isPicked = (line: CreditPick, performanceId: string | null) =>
    line.state === "picked" && line.performance_id === performanceId;

  return (
    <div>
      <PageHeader
        title="Credit picks"
        description="Lines whose log credits a different performance. Tick only the ones to skip, then press Apply once."
      />
      {error && (
        <p role="alert" className="mb-4 rounded-md bg-red-50 p-3 text-sm text-red-700">
          {error.message}
        </p>
      )}
      {apply.data && (
        <p role="status" className="mb-4 rounded-md bg-green-50 p-3 text-sm text-green-800">
          {apply.data.applied.toLocaleString()} applied, {apply.data.skipped.toLocaleString()}{" "}
          skipped, {apply.data.stale.length.toLocaleString()} stale
        </p>
      )}
      {credit.length === 0 && lines.length === 0 ? (
        <p className="text-sm text-gray-400">Nothing to pick.</p>
      ) : (
        <>
          <section className="mb-8">
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">
              Credits
            </h2>
            {credit.length === 0 ? (
              <p className="text-sm text-gray-400">No credits left to apply.</p>
            ) : (
              <>
                <div className="mb-3 flex items-center gap-3">
                  <button type="button" disabled={apply.isPending} onClick={applyAll}>
                    Apply
                  </button>
                  <span className="text-sm text-gray-500">
                    {applyIds.length.toLocaleString()} to apply, {skipIds.length.toLocaleString()}{" "}
                    to skip
                  </span>
                </div>
                <table aria-label="Credit picks to apply" className="w-full text-sm">
                  <thead>
                    <tr>
                      <th>Skip</th>
                      <th>Title</th>
                      <th>Artist</th>
                      <th>Plays</th>
                      <th>Plays now</th>
                      <th>Would play</th>
                    </tr>
                  </thead>
                  <tbody>
                    {credit.map((p) => (
                      <tr key={p.id} aria-label={nameOf(p)}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Skip ${p.title}`}
                            checked={skipped.has(p.id)}
                            disabled={apply.isPending}
                            onChange={() => toggle(p.id)}
                          />
                        </td>
                        <td>{p.title}</td>
                        <td>
                          {p.artist_name}
                          {p.reason === "shared_artist" && (
                            <span className="ml-2 rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
                              Shared artist
                            </span>
                          )}
                        </td>
                        <td>{p.plays.toLocaleString()}</td>
                        <FileCell file={p.plays_now} />
                        <FileCell file={p.would_play} />
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </section>
          <section>
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">
              Version lines
            </h2>
            {lines.length === 0 ? (
              <p className="text-sm text-gray-400">No version lines left to pick.</p>
            ) : (
              <table aria-label="Version lines to pick" className="w-full text-sm">
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Artist</th>
                    <th>Plays</th>
                    <th>Plays now</th>
                    <th>Pick</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((line) => (
                    <tr key={line.id} aria-label={nameOf(line)}>
                      <td>{line.title}</td>
                      <td>{line.artist_name}</td>
                      <td>{line.plays.toLocaleString()}</td>
                      <FileCell file={line.plays_now} />
                      <td>
                        <div
                          role="radiogroup"
                          aria-label={`Pick for ${nameOf(line)}`}
                          className="flex flex-col gap-1"
                        >
                          {line.candidates.map((candidate) => (
                            <label key={candidate.performance_id} className="flex gap-2">
                              <input
                                type="radio"
                                name={`pick-${line.id}`}
                                aria-label={candidateLabel(candidate)}
                                checked={isPicked(line, candidate.performance_id)}
                                onChange={() => choose(line, candidate.performance_id)}
                              />
                              {candidateLabel(candidate)}
                            </label>
                          ))}
                          <label className="flex gap-2">
                            <input
                              type="radio"
                              name={`pick-${line.id}`}
                              aria-label="None"
                              checked={isPicked(line, null)}
                              onChange={() => choose(line, null)}
                            />
                            None
                          </label>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
    </div>
  );
}
