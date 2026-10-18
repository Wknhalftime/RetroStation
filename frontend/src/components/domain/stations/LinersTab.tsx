import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, Pencil, Play, Square, Trash2, Upload } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import {
  fetchLinerAudio,
  useDeleteLiner,
  useRenameLiner,
  useSetLinerCadence,
  useStationLiners,
  useUploadLiner,
} from "@/api/liners";
import { ConflictError, ValidationError } from "@/api/client";
import { formatDuration } from "@/lib/utils";
import type { LinerCueState } from "@/lib/schemas/liners";
import {
  fromPicker,
  readDropped,
  takeDropped,
  type LinerCandidate,
} from "@/components/domain/stations/linerFiles";

const CADENCE_HINT = "Blank turns liners off. 1 to 1,000.";
const MIN_CADENCE = 1;
const MAX_CADENCE = 1000;

type BadgeVariant = "default" | "success" | "warning" | "danger" | "info";

const CUE_LABELS: Record<LinerCueState, { label: string; variant: BadgeVariant }> = {
  edited: { label: "Edited", variant: "success" },
  auto: { label: "Auto", variant: "default" },
  failed: { label: "Auto (analysis failed)", variant: "warning" },
  analysing: { label: "Analysing…", variant: "info" },
  silent: { label: "Silent: never plays", variant: "danger" },
};

// What the cadence box holds: off (blank), a whole number in range, or something else.
type CadenceDraft = { kind: "off" } | { kind: "songs"; songs: number } | { kind: "invalid" };

function parseCadence(text: string): CadenceDraft {
  const trimmed = text.trim();
  if (trimmed === "") return { kind: "off" };
  if (!/^\d+$/.test(trimmed)) return { kind: "invalid" };
  const songs = Number(trimmed);
  if (songs < MIN_CADENCE || songs > MAX_CADENCE) return { kind: "invalid" };
  return { kind: "songs", songs };
}

type UploadStatus =
  | { kind: "idle" }
  | { kind: "running"; done: number; total: number; current: string }
  | { kind: "done"; added: number; already: number; skipped: string[] };

// The server's own reason for refusing a file: a string detail, or a 422's detail[0].msg.
function refusalReason(err: unknown): string {
  if (err instanceof ValidationError && Array.isArray(err.detail) && err.detail.length > 0) {
    return err.detail[0].msg;
  }
  if (err instanceof Error) return err.message;
  return "upload failed";
}

export function LinersTab({ stationId }: { stationId: string }) {
  const { data, isLoading, isError } = useStationLiners(stationId);
  const cadenceMutation = useSetLinerCadence(stationId);
  const uploadMutation = useUploadLiner(stationId);
  const renameMutation = useRenameLiner(stationId);
  const deleteMutation = useDeleteLiner(stationId);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  const [cadenceText, setCadenceText] = useState("");
  const [syncedCadence, setSyncedCadence] = useState<{ value: number | null } | null>(null);
  const [upload, setUpload] = useState<UploadStatus>({ kind: "idle" });
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);

  const alive = useRef(true);
  const player = useRef<{ audio: HTMLAudioElement; url: string } | null>(null);
  const previewTicket = useRef(0);

  // The box follows the stored cadence whenever it changes (first load, after Save).
  if (data && (syncedCadence === null || syncedCadence.value !== data.songs_between_liners)) {
    setSyncedCadence({ value: data.songs_between_liners });
    setCadenceText(data.songs_between_liners === null ? "" : String(data.songs_between_liners));
  }

  // -------------------------------------------------------------------------
  // Preview: one liner at a time, through an object URL that is always revoked
  // -------------------------------------------------------------------------

  const releasePlayer = useCallback(() => {
    const current = player.current;
    player.current = null;
    if (!current) return;
    current.audio.pause();
    URL.revokeObjectURL(current.url);
  }, []);

  const stopPreview = useCallback(() => {
    previewTicket.current += 1;
    releasePlayer();
    setPlayingId(null);
  }, [releasePlayer]);

  useEffect(() => {
    alive.current = true;
    const ticket = previewTicket;
    return () => {
      alive.current = false;
      ticket.current += 1;
      releasePlayer();
    };
  }, [releasePlayer]);

  const togglePreview = async (linerId: string) => {
    if (playingId === linerId) {
      stopPreview();
      return;
    }
    stopPreview();
    const ticket = previewTicket.current;
    setNotice(null);
    setPlayingId(linerId);
    try {
      const blob = await fetchLinerAudio(stationId, linerId);
      if (ticket !== previewTicket.current) return;
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.addEventListener("ended", stopPreview);
      audio.addEventListener("error", stopPreview);
      player.current = { audio, url };
      await audio.play();
    } catch (err) {
      if (ticket !== previewTicket.current) return;
      stopPreview();
      setNotice(err instanceof Error ? `Preview failed: ${err.message}` : "Preview failed.");
    }
  };

  // -------------------------------------------------------------------------
  // Cadence
  // -------------------------------------------------------------------------

  const cadence = parseCadence(cadenceText);
  const cadenceValid = cadence.kind !== "invalid";

  const saveCadence = () => {
    if (cadence.kind === "invalid") return;
    setNotice(null);
    cadenceMutation.mutate(cadence.kind === "off" ? null : cadence.songs, {
      onError: (err) => setNotice(`Could not save the cadence: ${err.message}`),
    });
  };

  // -------------------------------------------------------------------------
  // Upload: every file is sent, one request at a time; the server decides what is audio
  // -------------------------------------------------------------------------

  const uploading = upload.kind === "running";

  const uploadAll = async (candidates: LinerCandidate[]) => {
    let added = 0;
    let already = 0;
    const skipped: string[] = [];
    setNotice(null);
    for (const [index, { file, path }] of candidates.entries()) {
      if (!alive.current) return;
      setUpload({ kind: "running", done: index, total: candidates.length, current: path });
      try {
        const result = await uploadMutation.mutateAsync(file);
        if (result.outcome === "added") added += 1;
        else already += 1;
      } catch (err) {
        if (err instanceof ConflictError) already += 1;
        else skipped.push(`${path}: ${refusalReason(err)}`);
      }
    }
    if (alive.current) setUpload({ kind: "done", added, already, skipped });
  };

  const handlePicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (picked.length === 0 || uploading) return;
    void uploadAll(fromPicker(picked));
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (!uploading) setDragging(true);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    if (uploading) return;
    // dataTransfer is only readable until this handler returns: take it before awaiting.
    const dropped = takeDropped(e.dataTransfer);
    readDropped(dropped)
      .then((candidates) => (candidates.length > 0 ? uploadAll(candidates) : undefined))
      .catch((err: unknown) => {
        const why = err instanceof Error ? `: ${err.message}` : ".";
        setNotice(`Could not read the drop${why}`);
      });
  };

  // -------------------------------------------------------------------------
  // Rename and delete
  // -------------------------------------------------------------------------

  const handleRename = (id: string, current: string) => {
    const next = window.prompt("Rename liner", current)?.trim();
    if (!next || next === current) return;
    setNotice(null);
    renameMutation.mutate(
      { id, name: next },
      { onError: (err) => setNotice(`Could not rename the liner: ${err.message}`) }
    );
  };

  const handleDelete = (id: string, name: string) => {
    if (!window.confirm(`Delete liner ${name}?`)) return;
    if (playingId === id) stopPreview();
    setNotice(null);
    deleteMutation.mutate(id, {
      onError: (err) => setNotice(`Could not delete the liner: ${err.message}`),
    });
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">The liners failed to load.</p>
    );
  }

  const linkClass = "font-medium text-indigo-600 hover:text-indigo-700 disabled:opacity-50";
  const iconButton =
    "rounded p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-50";

  return (
    <div className="space-y-6">
      {/* Cadence */}
      <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center gap-3">
          <label htmlFor="liner-cadence" className="text-sm font-medium text-gray-700">
            Songs between liners
          </label>
          <input
            id="liner-cadence"
            type="text"
            inputMode="numeric"
            value={cadenceText}
            placeholder="blank = off"
            onChange={(e) => setCadenceText(e.target.value)}
            className={[
              "w-28 rounded-md border px-3 py-1.5 text-sm",
              cadenceValid ? "border-gray-300" : "border-red-400",
            ].join(" ")}
          />
          <button
            type="button"
            onClick={saveCadence}
            disabled={!cadenceValid || cadenceMutation.isPending}
            className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            Save
          </button>
        </div>
        <p className={["mt-2 text-xs", cadenceValid ? "text-gray-500" : "text-red-600"].join(" ")}>
          {CADENCE_HINT}
        </p>
      </div>

      {/* Drop zone */}
      <div
        onDragOver={handleDragOver}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        className={[
          "rounded-xl border-2 border-dashed p-6 text-center text-sm text-gray-600",
          dragging ? "border-indigo-400 bg-indigo-50" : "border-gray-300 bg-white",
        ].join(" ")}
      >
        <Upload className="mx-auto mb-2 h-5 w-5 text-gray-400" />
        <p>Drop liner files or a folder here</p>
        <p className="mt-1 text-xs text-gray-500">
          or{" "}
          <button
            type="button"
            className={linkClass}
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            choose files
          </button>{" "}
          /{" "}
          <button
            type="button"
            className={linkClass}
            disabled={uploading}
            onClick={() => folderInputRef.current?.click()}
          >
            choose folder
          </button>
        </p>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="sr-only"
          tabIndex={-1}
          onChange={handlePicked}
        />
        <input
          ref={folderInputRef}
          type="file"
          className="sr-only"
          tabIndex={-1}
          onChange={handlePicked}
          {...({
            webkitdirectory: "",
            directory: "",
          } as React.InputHTMLAttributes<HTMLInputElement>)}
        />

        {upload.kind === "running" && (
          <div className="mt-3 flex items-center justify-center gap-2 rounded-md bg-blue-50 px-3 py-2 text-sm text-blue-700">
            <Spinner className="h-4 w-4 shrink-0" />
            {`Uploading ${upload.done + 1}/${upload.total}: ${upload.current}`}
          </div>
        )}
        {upload.kind === "done" && (
          <div className="mt-3 space-y-1 text-left text-sm text-gray-700">
            <p className="font-medium">
              {`Added ${upload.added} · Already there ${upload.already} · Skipped ${upload.skipped.length}`}
            </p>
            {upload.skipped.length > 0 && (
              <ul className="space-y-0.5 text-xs text-red-700">
                {upload.skipped.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {notice && (
        <div className="flex items-center gap-2 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {notice}
        </div>
      )}

      {/* Liners */}
      <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50 text-left text-xs font-medium uppercase tracking-wide text-gray-500">
            <tr>
              <th scope="col" className="px-4 py-3">
                Liner
              </th>
              <th scope="col" className="px-4 py-3">
                Length
              </th>
              <th scope="col" className="px-4 py-3">
                Cues
              </th>
              <th scope="col" className="px-4 py-3">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.liners.length === 0 && (
              <tr>
                <td colSpan={4} className="px-4 py-6 text-center text-gray-500">
                  No liners yet.
                </td>
              </tr>
            )}
            {data.liners.map((liner) => {
              const cue = CUE_LABELS[liner.cues];
              const playing = playingId === liner.id;
              return (
                <tr key={liner.id}>
                  <td className="px-4 py-2.5 text-gray-900">{liner.name}</td>
                  <td className="px-4 py-2.5 tabular-nums text-gray-600">
                    {formatDuration(liner.duration_ms)}
                  </td>
                  <td className="px-4 py-2.5">
                    <Badge variant={cue.variant}>{cue.label}</Badge>
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        type="button"
                        aria-label="Preview"
                        title={playing ? "Stop preview" : "Preview"}
                        onClick={() => void togglePreview(liner.id)}
                        className={iconButton}
                      >
                        {playing ? <Square className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                      </button>
                      <Link
                        to={`/stations/${stationId}/liners/${liner.id}/cues`}
                        className="px-2 text-sm font-medium text-indigo-600 hover:text-indigo-700"
                      >
                        Edit cues
                      </Link>
                      <button
                        type="button"
                        aria-label="Rename"
                        title="Rename"
                        onClick={() => handleRename(liner.id, liner.name)}
                        className={iconButton}
                      >
                        <Pencil className="h-4 w-4" />
                      </button>
                      <button
                        type="button"
                        aria-label="Delete"
                        title="Delete"
                        onClick={() => handleDelete(liner.id, liner.name)}
                        className={iconButton}
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
