import { useMemo, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { formatMs, previewRange, type MarkerKey, type Markers, type View } from "./cueMath";
import type { CuePreview } from "./useCuePreview";
import { WaveformPane, type BoxKey } from "./WaveformPane";

// The editor's three waveform views as the approved mockup lays them out: the whole song on top
// with the Start view and End view boxes, then the two close-ups side by side, each with its
// preview controls. Prop-driven (peaks, markers, views and a preview in; changes out).

const WHOLE_HEIGHT = 100;
const CLOSE_UP_HEIGHT = 130;

export interface CueWaveformsProps {
  peaks: Uint8Array;
  perSecond: number;
  durationMs: number;
  markers: Markers;
  views: { start: View; end: View };
  preview: CuePreview;
  /** A line under the views while the preview is not ready (e.g. "Loading audio…"). */
  status: string | null;
  onMarker(key: MarkerKey, ms: number): void;
  onView(which: BoxKey, v: View): void;
}

function PaneLabel({ children }: { children: ReactNode }) {
  return (
    <div className="mb-1 text-xs font-medium tracking-wide text-gray-500 uppercase">{children}</div>
  );
}

function Showing({ view }: { view: View }) {
  const seconds = (view.to - view.from) / 1000;
  return (
    <span className="ml-1.5 tracking-normal normal-case opacity-70">
      showing {seconds.toFixed(seconds < 2 ? 2 : 1)} s
    </span>
  );
}

const BUTTON = cn(
  "rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700",
  "hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
);

export function CueWaveforms(props: CueWaveformsProps) {
  const { peaks, perSecond, durationMs, markers, views, preview, status, onMarker, onView } = props;
  const whole = useMemo(() => ({ from: 0, to: durationMs }), [durationMs]);
  const startRange = previewRange("start", markers, views.end);
  const endRange = previewRange("end", markers, views.end);
  const playhead = preview.playheadMs === null ? "—" : formatMs(preview.playheadMs);
  const eomLit =
    preview.eomReached || (preview.playheadMs !== null && preview.playheadMs >= markers.eom);
  const shared = {
    peaks,
    perSecond,
    durationMs,
    markers,
    playheadMs: preview.playheadMs,
    onMarker,
  };

  return (
    <div className="space-y-4">
      <div>
        <PaneLabel>Whole song</PaneLabel>
        <WaveformPane
          {...shared}
          label="Whole song"
          height={WHOLE_HEIGHT}
          view={whole}
          labels={false}
          boxes={views}
          onBox={onView}
        />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <PaneLabel>
            Start <Showing view={views.start} />
          </PaneLabel>
          <WaveformPane
            {...shared}
            label="Start view"
            height={CLOSE_UP_HEIGHT}
            view={views.start}
            labels
            onView={(v) => onView("start", v)}
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              className={BUTTON}
              disabled={!preview.ready || !(startRange.to > startRange.from)}
              onClick={() => preview.play(startRange, markers)}
            >
              ▶ Play from Start
            </button>
            <button
              type="button"
              className={BUTTON}
              title="Stop the preview"
              onClick={preview.stop}
            >
              ■
            </button>
            <span className="font-mono text-xs text-gray-500">{formatMs(markers.start)}</span>
          </div>
        </div>

        <div>
          <PaneLabel>
            End <Showing view={views.end} />
          </PaneLabel>
          <WaveformPane
            {...shared}
            label="End view"
            height={CLOSE_UP_HEIGHT}
            view={views.end}
            labels
            onView={(v) => onView("end", v)}
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              className={BUTTON}
              disabled={!preview.ready || !(endRange.to > endRange.from)}
              onClick={() => preview.play(endRange, markers)}
            >
              ▶ Play End cue
            </button>
            <button
              type="button"
              className={BUTTON}
              title="Stop the preview"
              onClick={preview.stop}
            >
              ■
            </button>
            <span className="flex items-center gap-1.5 text-xs text-gray-700">
              <span
                aria-hidden="true"
                data-lit={eomLit}
                className={cn(
                  "inline-block h-4 w-4 rounded-full border",
                  eomLit
                    ? "border-amber-500 bg-amber-400 shadow-[0_0_10px_#ffb300]"
                    : "border-gray-400 bg-gray-300"
                )}
              />
              EOM
            </span>
          </div>
        </div>
      </div>

      <p className="font-mono text-xs text-gray-500">{status ?? `playhead ${playhead}`}</p>
    </div>
  );
}
