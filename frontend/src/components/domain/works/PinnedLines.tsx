import { useState } from "react";
import type { PinnedLine } from "@/lib/schemas/works";
import { basename } from "./CopyRow";

// The lines a copy holds through a merge pin (3b PR 5a, D41 d; C72), collapsed, counted by
// line. Each line shows its stations, plays and what it plays next; Release frees it at once.

export interface PinnedLineGroup {
  identityId: string;
  title: string;
  artist: string;
  stations: PinnedLine[];
}

/** The pins of one copy, one group per line, in the server's order. */
export function byLine(pins: PinnedLine[]): PinnedLineGroup[] {
  const groups = new Map<string, PinnedLineGroup>();
  for (const pin of pins) {
    const group = groups.get(pin.identity_id) ?? {
      identityId: pin.identity_id,
      title: pin.title,
      artist: pin.artist,
      stations: [],
    };
    group.stations.push(pin);
    groups.set(pin.identity_id, group);
  }
  return [...groups.values()];
}

export function plays(n: number): string {
  return n === 1 ? "1 play" : `${n} plays`;
}

interface PinnedLinesProps {
  pins: PinnedLine[];
  onRelease: (identityId: string) => void;
  releasing: boolean;
}

export function PinnedLines({ pins, onRelease, releasing }: PinnedLinesProps) {
  const [open, setOpen] = useState(false);
  const lines = byLine(pins);
  if (lines.length === 0) return null;
  return (
    <div className="bg-gray-50 px-4 py-2 pl-12 text-xs text-gray-600">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="font-medium text-indigo-600 hover:underline"
      >
        Pinned lines ({lines.length})
      </button>
      {open && (
        <ul className="mt-2 space-y-2">
          {lines.map((line) => (
            <li key={line.identityId} aria-label={line.title} className="space-y-0.5">
              <div className="flex items-center gap-2">
                <span className="font-medium text-gray-800">{line.title}</span>
                <span className="text-gray-400">{line.artist}</span>
                <button
                  type="button"
                  aria-label={`Release ${line.title}`}
                  disabled={releasing}
                  onClick={() => onRelease(line.identityId)}
                  className="ml-auto rounded border border-gray-300 px-1.5 py-0.5 hover:bg-white disabled:opacity-60"
                >
                  Release
                </button>
              </div>
              {line.stations.map((pin) => (
                <div key={pin.station_id}>
                  {pin.station} · {plays(pin.plays)} · plays next:{" "}
                  {pin.next_path ? basename(pin.next_path) : "nothing"}
                </div>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
