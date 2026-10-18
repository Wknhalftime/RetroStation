import { useCallback, useEffect, useMemo, useState } from "react";
import type { UseMutationResult, UseQueryResult } from "@tanstack/react-query";
import { Spinner } from "@/components/ui/Spinner";
import { cn, formatDuration } from "@/lib/utils";
import {
  decodePeaks,
  type ApiMarkers,
  type CueEditor as CueEditorView,
  type Waveform,
} from "@/lib/schemas/cues";
import {
  defaultViews,
  fromApi,
  placeMarker,
  placeMarkerFadeAtStop,
  sameMarkers,
  toApi,
  type MarkerKey,
  type Markers,
  type View,
} from "./cueMath";
import { CueMarkerTable } from "./CueMarkerTable";
import { CueWaveforms } from "./CueWaveforms";
import { errorText } from "./errorText";
import { MARKER_ORDER } from "./markerStyle";
import { useCuePreview } from "./useCuePreview";
import type { BoxKey } from "./WaveformPane";

// The cue editor itself, for whatever is being edited (a library song, a station liner): the
// markers' state, the badge, the waveforms, the table, Save and Reset, and the unsaved-edit
// guard. The page fetches; this is told the editor view, the waveform and audio queries, and
// the save and reset mutations it built.

const NO_PEAKS = new Uint8Array(0);
// A liner has no fade: its Fade-out is pinned to Stop and shown nowhere.
const NO_FADE_KEYS: readonly MarkerKey[] = ["start", "eom", "stop"];

const BUTTON =
  "rounded-md border px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";

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

/** Markers as the editor holds them: with lockFade, Fade-out sits at Stop. */
function held(api: ApiMarkers, lockFade: boolean): Markers {
  const markers = fromApi(api);
  return lockFade ? { ...markers, fadeAt: markers.stop } : markers;
}

export interface CueEditorBodyProps {
  editor: CueEditorView;
  waveform: UseQueryResult<Waveform>;
  audio: UseQueryResult<ArrayBuffer>;
  save: UseMutationResult<void, Error, ApiMarkers>;
  reset: UseMutationResult<void, Error, void>;
  /** Hide Fade-out everywhere and keep it at Stop after every change (a liner). */
  lockFade: boolean;
  /** Shown under the table when the audio was measured silent. */
  silentNote: string;
}

export function CueEditorBody(props: CueEditorBodyProps) {
  const { editor, waveform, audio, save, reset, lockFade, silentNote } = props;
  const durationMs = editor.duration_ms;
  const silent = editor.analysis === "silent";
  const markerKeys = lockFade ? NO_FADE_KEYS : MARKER_ORDER;
  const place = lockFade ? placeMarkerFadeAtStop : placeMarker;
  const automatic = useMemo(() => held(editor.automatic, lockFade), [editor.automatic, lockFade]);
  const [markers, setMarkers] = useState<Markers>(() =>
    held(editor.manual ?? editor.automatic, lockFade)
  );
  const [saved, setSaved] = useState<Markers>(markers);
  const [views, setViews] = useState(() => defaultViews(markers, durationMs));

  const preview = useCuePreview(audio.data);
  const peaks = useMemo(() => readPeaks(waveform.data), [waveform.data]);

  const onMarker = useCallback(
    (key: MarkerKey, ms: number) => setMarkers((m) => place(m, key, ms, durationMs)),
    [place, durationMs]
  );
  const onNudge = useCallback(
    (key: MarkerKey, deltaMs: number) =>
      setMarkers((m) => place(m, key, m[key] + deltaMs, durationMs)),
    [place, durationMs]
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
  const busy = save.isPending || reset.isPending;

  // Closing the tab or reloading with unsaved edits asks the browser to confirm first.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
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
          markerKeys={markerKeys}
        />
      </section>

      <section className="space-y-4 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <CueMarkerTable
          markers={markers}
          automatic={automatic}
          onNudge={onNudge}
          markerKeys={markerKeys}
        />
        {silent && (
          <p role="note" className="rounded-md bg-amber-50 p-3 text-sm text-amber-800">
            {silentNote}
          </p>
        )}
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onSave}
            disabled={silent || !dirty || busy}
            className={cn(BUTTON, "border-indigo-600 bg-indigo-600 text-white hover:bg-indigo-700")}
          >
            Save
          </button>
          <button
            type="button"
            onClick={onReset}
            disabled={busy}
            className={cn(BUTTON, "border-gray-300 bg-white text-gray-700 hover:bg-gray-50")}
          >
            Reset to automatic
          </button>
          {busy && <Spinner className="h-4 w-4 text-indigo-500" />}
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
