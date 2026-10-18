import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import { cn, formatDuration } from "@/lib/utils";
import { ValidationError } from "@/api/client";
import { useCueAudio, useCueEditor, useResetCues, useSaveCues, useWaveform } from "@/api/cues";
import { decodePeaks, type CueEditor as CueEditorView, type Waveform } from "@/lib/schemas/cues";
import {
  defaultViews,
  fromApi,
  nudge,
  placeMarker,
  sameMarkers,
  toApi,
  type MarkerKey,
  type Markers,
  type View,
} from "@/components/domain/cues/cueMath";
import { CueMarkerTable } from "@/components/domain/cues/CueMarkerTable";
import { CueWaveforms } from "@/components/domain/cues/CueWaveforms";
import { useCuePreview } from "@/components/domain/cues/useCuePreview";
import type { BoxKey } from "@/components/domain/cues/WaveformPane";

// The cue editor page (/library/files/:file_id/cues): Lance sets Start, EOM, Fade-out and Stop
// for one file's audio on its waveform, previews the start and the end, and saves (R8). This page
// is the only part that fetches; the waveform, the preview and the math are prop-driven.

const SILENT_NOTE = "Measured silent — the radio skips this song, so its cues can't be edited.";
const NO_PEAKS = new Uint8Array(0);

const BUTTON =
  "rounded-md border px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";

/** The text to show for a failed request: the server's reason where it gave one. */
function errorText(error: Error): string {
  if (error instanceof ValidationError) {
    // FastAPI sends a list of {msg}; a hand-raised 422 may send a plain string.
    const detail: unknown = error.detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail)) {
      const reasons = detail
        .map((item: unknown) =>
          typeof item === "object" && item !== null && "msg" in item ? String(item.msg) : ""
        )
        .filter(Boolean);
      if (reasons.length > 0) return reasons.join(" ");
    }
  }
  return error.message;
}

type Peaks = { peaks: Uint8Array; perSecond: number; error: string | null };

/** The waveform's peaks, or an error instead of a crash when the payload is malformed. */
function readPeaks(waveform: Waveform | undefined): Peaks {
  if (!waveform) return { peaks: NO_PEAKS, perSecond: 1, error: null };
  const malformed = { peaks: NO_PEAKS, perSecond: 1, error: "The waveform couldn't be read." };
  if (!(waveform.per_second > 0)) return malformed;
  try {
    return { peaks: decodePeaks(waveform.peaks), perSecond: waveform.per_second, error: null };
  } catch (error) {
    if (error instanceof Error) return malformed;
    throw error;
  }
}

function CueEditorBody({ fileId, editor }: { fileId: string; editor: CueEditorView }) {
  const durationMs = editor.duration_ms;
  const silent = editor.analysis === "silent";
  const automatic = useMemo(() => fromApi(editor.automatic), [editor.automatic]);
  const [markers, setMarkers] = useState<Markers>(() => fromApi(editor.manual ?? editor.automatic));
  const [saved, setSaved] = useState<Markers>(markers);
  const [views, setViews] = useState(() => defaultViews(markers, durationMs));

  const waveform = useWaveform(fileId);
  const audio = useCueAudio(fileId);
  const preview = useCuePreview(audio.data);
  const save = useSaveCues(fileId);
  const reset = useResetCues(fileId);
  const peaks = useMemo(() => readPeaks(waveform.data), [waveform.data]);

  const onMarker = useCallback(
    (key: MarkerKey, ms: number) => setMarkers((m) => placeMarker(m, key, ms, durationMs)),
    [durationMs]
  );
  const onNudge = useCallback(
    (key: MarkerKey, deltaMs: number) => setMarkers((m) => nudge(m, key, deltaMs, durationMs)),
    [durationMs]
  );
  const onView = useCallback(
    (which: BoxKey, v: View) => setViews((current) => ({ ...current, [which]: v })),
    []
  );

  function onSave() {
    const submitted = markers;
    save.mutate(toApi(submitted), { onSuccess: () => setSaved(submitted) });
  }

  function onReset() {
    reset.mutate(undefined, {
      onSuccess: () => {
        setMarkers(automatic);
        setSaved(automatic);
        save.reset();
      },
    });
  }

  const isManual = !sameMarkers(markers, automatic);
  const dirty = !sameMarkers(markers, saved);
  const waveformError = waveform.error ? errorText(waveform.error) : peaks.error;
  let status: string | null = null;
  if (audio.error) status = `The audio couldn't be loaded: ${errorText(audio.error)}`;
  else if (preview.error) status = preview.error;
  else if (!preview.ready) status = "Loading audio…";

  return (
    <>
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-lg text-gray-900">
            <strong className="font-semibold">{editor.title ?? "Untitled"}</strong> ·{" "}
            {formatDuration(durationMs)} · {editor.format.toUpperCase()}
          </p>
          {editor.artist && <p className="text-sm text-gray-500">{editor.artist}</p>}
        </div>
        <span
          className={cn(
            "rounded-full px-2.5 py-0.5 text-xs font-medium",
            isManual ? "bg-amber-100 text-amber-800" : "bg-green-100 text-green-800"
          )}
        >
          {isManual ? "Manual" : "Automatic"}
        </span>
      </div>

      <section className="space-y-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        {waveformError && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
            {waveformError}
          </p>
        )}
        <CueWaveforms
          peaks={peaks.peaks}
          perSecond={peaks.perSecond}
          durationMs={durationMs}
          markers={markers}
          views={views}
          preview={preview}
          status={status}
          onMarker={onMarker}
          onView={onView}
        />
      </section>

      <section className="space-y-4 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <CueMarkerTable markers={markers} automatic={automatic} onNudge={onNudge} />
        {silent && (
          <p role="note" className="rounded-md bg-amber-50 p-3 text-sm text-amber-800">
            {SILENT_NOTE}
          </p>
        )}
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onSave}
            disabled={silent || !dirty || save.isPending}
            className={cn(BUTTON, "border-indigo-600 bg-indigo-600 text-white hover:bg-indigo-700")}
          >
            Save
          </button>
          <button
            type="button"
            onClick={onReset}
            disabled={reset.isPending}
            className={cn(BUTTON, "border-gray-300 bg-white text-gray-700 hover:bg-gray-50")}
          >
            Reset to automatic
          </button>
          {(save.isPending || reset.isPending) && <Spinner className="h-4 w-4 text-indigo-500" />}
        </div>
        {save.error && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
            {errorText(save.error)}
          </p>
        )}
        {reset.error && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
            {errorText(reset.error)}
          </p>
        )}
      </section>
    </>
  );
}

export function CueEditor() {
  const { file_id: fileId = "" } = useParams();
  const navigate = useNavigate();
  const editor = useCueEditor(fileId);

  let body;
  if (editor.isError) {
    body = (
      <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
        {errorText(editor.error)}
      </p>
    );
  } else if (!editor.data) {
    body = (
      <div className="flex justify-center py-12">
        <Spinner className="h-6 w-6 text-indigo-500" />
      </div>
    );
  } else {
    body = <CueEditorBody key={fileId} fileId={fileId} editor={editor.data} />;
  }

  return (
    <div className="space-y-6">
      <div>
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to the song
        </button>
      </div>
      {body}
    </div>
  );
}
