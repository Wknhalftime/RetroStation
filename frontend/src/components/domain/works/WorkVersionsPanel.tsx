import { useState } from "react";
import { useDismissVersions, useMapVersions, useUnmapVersions } from "@/api/versions";
import type { RecordingDetail } from "@/lib/schemas/works";
import type { MasterFlag, WorkVersion } from "@/lib/schemas/versions";

// Versions phase 2b PR 3, SPEC S6 (M30, C62, C64, C66): the version texts of the work's matched
// lines, beside the format overrides. Map one or more to a file of the work, Unmap, Dismiss;
// a suggestion maps only when pressed.

interface WorkVersionsPanelProps {
  workId: string;
  versions: WorkVersion[];
  masterFlag: MasterFlag | null;
  recordings: RecordingDetail[];
}

const STATE_LABEL = { unmapped: "Unmapped", mapped: "Mapped", dismissed: "Dismissed" } as const;

function fileLabels(recordings: RecordingDetail[]): Map<string, string> {
  const labels = new Map<string, string>();
  for (const rec of recordings) {
    for (const f of rec.files) labels.set(f.id, `${rec.title} — ${f.format.toUpperCase()}`);
  }
  return labels;
}

export function WorkVersionsPanel({
  workId,
  versions,
  masterFlag,
  recordings,
}: WorkVersionsPanelProps) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [fileId, setFileId] = useState("");
  const mapVersions = useMapVersions();
  const unmapVersions = useUnmapVersions();
  const dismissVersions = useDismissVersions();
  const busy = mapVersions.isPending || unmapVersions.isPending || dismissVersions.isPending;
  const error = mapVersions.error ?? unmapVersions.error ?? dismissVersions.error;

  const labels = fileLabels(recordings);
  const mappable = recordings.flatMap((rec) =>
    rec.files
      .filter((f) => f.file_status === "present" && !f.other_work)
      .map((f) => ({ id: f.id, label: `${rec.title} — ${f.format.toUpperCase()}` }))
  );
  const versionRows = versions.filter((v) => v.reads_as_version);
  const otherRows = versions.filter((v) => !v.reads_as_version);

  const toggle = (text: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(text)) next.delete(text);
      else next.add(text);
      return next;
    });
  const resetAll = () => {
    mapVersions.reset();
    unmapVersions.reset();
    dismissVersions.reset();
  };
  const mapSelected = () => {
    resetAll();
    mapVersions.mutate(
      {
        workId,
        versionTexts: versions.map((v) => v.version_text).filter((t) => selected.has(t)),
        fileId,
      },
      { onSuccess: () => setSelected(new Set()) }
    );
  };

  return (
    <div className="rounded-xl border border-gray-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3">
        <h3 className="text-sm font-semibold text-gray-700">Versions</h3>
      </div>

      {masterFlag && (
        <p className="border-b border-amber-100 bg-amber-50 px-4 py-2 text-sm text-amber-800">
          The master reads as {masterFlag.version_type}, and this work also holds a plain copy. Set
          master can pick it for the plain lines.
        </p>
      )}

      {error && (
        <p role="alert" className="bg-red-50 px-4 py-2 text-sm text-red-700">
          {error.message}
        </p>
      )}

      <div className="space-y-3 px-4 py-3">
        {versions.length === 0 ? (
          <p className="text-sm text-gray-400">No version texts on this work's matched lines.</p>
        ) : (
          <>
            <ul className="space-y-2">
              {versionRows.map((v) => (
                <li key={v.version_text} aria-label={v.version_text} className="text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    {v.state !== "mapped" && (
                      <input
                        type="checkbox"
                        aria-label={`Select ${v.version_text}`}
                        checked={selected.has(v.version_text)}
                        onChange={() => toggle(v.version_text)}
                      />
                    )}
                    <span className="font-medium text-gray-900">{v.version_text}</span>
                    <span className="text-gray-500">{v.version_type}</span>
                    <span className="text-gray-500">{v.plays.toLocaleString()} plays</span>
                    <span className="text-gray-400">{v.stations.join(", ")}</span>
                    <span className="rounded bg-gray-100 px-1.5 text-xs">
                      {STATE_LABEL[v.state]}
                    </span>
                    {v.state === "mapped" && v.mapped_file_id && (
                      <>
                        <span className="text-gray-600">
                          → {labels.get(v.mapped_file_id) ?? v.mapped_file_id}
                        </span>
                        <button
                          type="button"
                          aria-label={`Unmap ${v.version_text}`}
                          disabled={busy}
                          onClick={() => {
                            resetAll();
                            unmapVersions.mutate({ workId, versionTexts: [v.version_text] });
                          }}
                        >
                          Unmap
                        </button>
                      </>
                    )}
                    {v.state === "unmapped" && (
                      <button
                        type="button"
                        aria-label={`Dismiss ${v.version_text}`}
                        disabled={busy}
                        onClick={() => {
                          resetAll();
                          dismissVersions.mutate({ workId, versionTexts: [v.version_text] });
                        }}
                      >
                        Dismiss
                      </button>
                    )}
                  </div>
                  {v.new && (
                    <p className="text-xs text-indigo-700">
                      New version detected — first on {v.first_station ?? "a station"},{" "}
                      {v.first_seen ?? "date unknown"}
                    </p>
                  )}
                  {v.suggestion && (
                    <p className="text-xs text-gray-600">
                      Suggested:{" "}
                      {labels.get(v.suggestion.file_id) ??
                        v.suggestion.track_title ??
                        v.suggestion.file_path}{" "}
                      <button
                        type="button"
                        aria-label={`Use suggestion for ${v.version_text}`}
                        disabled={busy}
                        onClick={() => {
                          resetAll();
                          mapVersions.mutate({
                            workId,
                            versionTexts: [v.version_text],
                            fileId: v.suggestion!.file_id,
                          });
                        }}
                      >
                        Use suggestion
                      </button>
                    </p>
                  )}
                </li>
              ))}
            </ul>

            <div className="flex items-center gap-2">
              <select
                aria-label="Map to file"
                value={fileId}
                onChange={(e) => setFileId(e.target.value)}
                className="rounded-md border border-gray-300 px-2 py-1 text-sm"
              >
                <option value="">Select a file...</option>
                {mappable.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                disabled={busy || selected.size === 0 || !fileId}
                onClick={mapSelected}
              >
                Map selected
              </button>
            </div>

            {otherRows.length > 0 && (
              <>
                <h4 className="pt-2 text-xs font-semibold uppercase text-gray-500">Title words</h4>
                <ul className="space-y-1">
                  {otherRows.map((v) => (
                    <li
                      key={v.version_text}
                      aria-label={v.version_text}
                      className="text-sm text-gray-600"
                    >
                      {v.state !== "mapped" && (
                        <input
                          type="checkbox"
                          aria-label={`Select ${v.version_text}`}
                          checked={selected.has(v.version_text)}
                          onChange={() => toggle(v.version_text)}
                        />
                      )}{" "}
                      {v.version_text} · {v.plays.toLocaleString()} plays
                      {v.state === "mapped" && v.mapped_file_id && (
                        <>
                          {" "}
                          → {labels.get(v.mapped_file_id) ?? v.mapped_file_id}{" "}
                          <button
                            type="button"
                            aria-label={`Unmap ${v.version_text}`}
                            disabled={busy}
                            onClick={() => {
                              resetAll();
                              unmapVersions.mutate({ workId, versionTexts: [v.version_text] });
                            }}
                          >
                            Unmap
                          </button>
                        </>
                      )}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
