import { useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import {
  useDismissListed,
  useDismissVersions,
  useMapVersions,
  useUnmappedVersions,
} from "@/api/versions";
import type { UnmappedVersion } from "@/lib/schemas/versions";

// Versions phase 2b PR 3, SPEC S7 (C62): every unmapped version text, by plays.

const PAGE_SIZE = 50;

const keyOf = (v: UnmappedVersion) => `${v.work_id}\u0000${v.version_text}`;
const nameOf = (v: UnmappedVersion) => `${v.work_title} (${v.version_text})`;

export function UnmappedVersions() {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const { data, isLoading, isError } = useUnmappedVersions(offset, PAGE_SIZE);
  const mapVersions = useMapVersions();
  const dismissVersions = useDismissVersions();
  const dismissListed = useDismissListed();
  const busy = mapVersions.isPending || dismissVersions.isPending || dismissListed.isPending;
  const error = mapVersions.error ?? dismissVersions.error ?? dismissListed.error;

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
        Failed to load unmapped versions. Please try again.
      </p>
    );
  }

  const items = data.items;
  const chosen = items.filter((v) => selected.has(keyOf(v)));
  const toggle = (v: UnmappedVersion) =>
    setSelected((current) => {
      const next = new Set(current);
      const key = keyOf(v);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const dismissAll = () => {
    if (
      !window.confirm(`Dismiss all ${data.total} unmapped versions? Their lines keep the master.`)
    )
      return;
    dismissListed.mutate({ all: true });
  };

  return (
    <div>
      <PageHeader
        title="Unmapped versions"
        description="Version texts the stations wrote that no file is mapped to: their lines play the master."
      />
      {error && (
        <p role="alert" className="mb-4 rounded-md bg-red-50 p-3 text-sm text-red-700">
          {error.message}
        </p>
      )}
      {items.length === 0 ? (
        <p className="text-sm text-gray-400">No unmapped versions.</p>
      ) : (
        <>
          <div className="mb-3 flex gap-2">
            <button
              type="button"
              disabled={busy || !chosen.some((v) => v.suggestion)}
              onClick={() => {
                for (const v of chosen) {
                  if (!v.suggestion) continue;
                  mapVersions.mutate({
                    workId: v.work_id,
                    versionTexts: [v.version_text],
                    fileId: v.suggestion.file_id,
                  });
                }
              }}
            >
              Map selected
            </button>
            <button
              type="button"
              disabled={busy || chosen.length === 0}
              onClick={() =>
                dismissListed.mutate(
                  {
                    entries: chosen.map((v) => ({
                      work_id: v.work_id,
                      version_text: v.version_text,
                    })),
                  },
                  { onSuccess: () => setSelected(new Set()) }
                )
              }
            >
              Dismiss selected
            </button>
            <button type="button" disabled={busy} onClick={dismissAll}>
              Dismiss all
            </button>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th />
                <th>Artist</th>
                <th>Work</th>
                <th>Version</th>
                <th>Plays</th>
                <th>Stations</th>
                <th>First seen</th>
                <th>Suggested</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((v) => (
                <tr key={keyOf(v)} aria-label={nameOf(v)}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${nameOf(v)}`}
                      checked={selected.has(keyOf(v))}
                      onChange={() => toggle(v)}
                    />
                  </td>
                  <td>{v.artist_name}</td>
                  <td>{v.work_title}</td>
                  <td>
                    {v.version_text} {v.new && <span className="text-indigo-700">New</span>}
                  </td>
                  <td>{v.plays.toLocaleString()}</td>
                  <td>{v.stations.join(", ")}</td>
                  <td>{v.first_seen ?? ""}</td>
                  <td>
                    {v.suggestion ? (v.suggestion.track_title ?? v.suggestion.file_path) : ""}
                  </td>
                  <td className="space-x-2">
                    {v.suggestion && (
                      <button
                        type="button"
                        aria-label={`Map ${v.version_text} on ${v.work_title}`}
                        disabled={busy}
                        onClick={() =>
                          mapVersions.mutate({
                            workId: v.work_id,
                            versionTexts: [v.version_text],
                            fileId: v.suggestion!.file_id,
                          })
                        }
                      >
                        Map
                      </button>
                    )}
                    <button
                      type="button"
                      aria-label={`Dismiss ${v.version_text} on ${v.work_title}`}
                      disabled={busy}
                      onClick={() =>
                        dismissVersions.mutate({
                          workId: v.work_id,
                          versionTexts: [v.version_text],
                        })
                      }
                    >
                      Dismiss
                    </button>
                    <Link
                      to={`/library/artists/${v.artist_id}/works/${v.work_id}`}
                      aria-label={`Open ${v.work_title}`}
                    >
                      Open work
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <div className="mt-3 flex gap-2">
        <button type="button" disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>
          Previous
        </button>
        <button
          type="button"
          disabled={offset + PAGE_SIZE >= data.total}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next
        </button>
      </div>
    </div>
  );
}
