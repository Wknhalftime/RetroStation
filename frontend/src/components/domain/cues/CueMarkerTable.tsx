import { formatMs, type MarkerKey, type Markers } from "./cueMath";
import { MARKER_COLOR, MARKER_LABEL, MARKER_ORDER } from "./markerStyle";

// The four markers as numbers: each one's time, buttons that nudge it, and what autocue set it to.

const NUDGES: readonly { label: string; deltaMs: number }[] = [
  { label: "-1", deltaMs: -1000 },
  { label: "-0.1", deltaMs: -100 },
  { label: "-0.01", deltaMs: -10 },
  { label: "+0.01", deltaMs: 10 },
  { label: "+0.1", deltaMs: 100 },
  { label: "+1", deltaMs: 1000 },
];

const NUDGE_BUTTON =
  "rounded border border-gray-200 px-1.5 py-0.5 font-mono text-xs text-gray-700 hover:bg-gray-50";

interface CueMarkerTableProps {
  markers: Markers;
  automatic: Markers;
  onNudge(key: MarkerKey, deltaMs: number): void;
}

export function CueMarkerTable({ markers, automatic, onNudge }: CueMarkerTableProps) {
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-xs font-medium tracking-wide text-gray-500 uppercase">
          <th className="py-2 pr-4">Marker</th>
          <th className="py-2 pr-4">Time</th>
          <th className="py-2 pr-4">Nudge</th>
          <th className="py-2">Automatic was</th>
        </tr>
      </thead>
      <tbody>
        {MARKER_ORDER.map((key) => (
          <tr key={key} className="border-t border-gray-100">
            <td className="py-2 pr-4 whitespace-nowrap text-gray-900">
              <span aria-hidden="true" style={{ color: MARKER_COLOR[key] }}>
                ●
              </span>{" "}
              {MARKER_LABEL[key]}
            </td>
            <td className="py-2 pr-4 font-mono text-gray-900">{formatMs(markers[key])}</td>
            <td className="py-2 pr-4">
              <div className="flex flex-wrap gap-1">
                {NUDGES.map((n) => (
                  <button
                    key={n.label}
                    type="button"
                    onClick={() => onNudge(key, n.deltaMs)}
                    className={NUDGE_BUTTON}
                  >
                    {n.label}
                  </button>
                ))}
              </div>
            </td>
            <td className="py-2 font-mono text-gray-400">{formatMs(automatic[key])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
