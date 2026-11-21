import { useState } from "react";
import type { PinnedLine } from "@/lib/schemas/works";
import { basename } from "./CopyRow";
import { byLine, plays } from "./PinnedLines";

// C72 (Lance, 2026-10-29; 3b PR 5a, D41 e): before Set master replaces a copy that holds pins,
// its pinned lines, each with a tick, unticked. A ticked line is released (it then plays the
// new master); an unticked one stays pinned on the replaced copy.

interface SetMasterDialogProps {
  pins: PinnedLine[];
  oldPath: string;
  newPath: string;
  busy: boolean;
  onConfirm: (releaseIdentityIds: string[]) => void;
  onCancel: () => void;
}

export function SetMasterDialog({
  pins,
  oldPath,
  newPath,
  busy,
  onConfirm,
  onCancel,
}: SetMasterDialogProps) {
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());
  const lines = byLine(pins);
  const toggle = (id: string) =>
    setTicked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Set master"
      className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 shadow-sm"
    >
      <p className="font-medium">
        The copy this replaces holds pinned lines. Released lines play {basename(newPath)}. Kept
        lines stay on {basename(oldPath)}.
      </p>
      <ul className="my-3 space-y-2">
        {lines.map((line) => (
          <li key={line.identityId}>
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={ticked.has(line.identityId)}
                onChange={() => toggle(line.identityId)}
                aria-label={`${line.title} on ${line.stations.map((s) => s.station).join(", ")}`}
              />
              <span>
                <span className="font-medium">{line.title}</span>{" "}
                <span className="text-amber-700">{line.artist}</span>
                {line.stations.map((pin) => (
                  <span key={pin.station_id} className="block text-xs">
                    {pin.station} · {plays(pin.plays)}
                  </span>
                ))}
              </span>
            </label>
          </li>
        ))}
      </ul>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => onConfirm([...ticked])}
          className="rounded-md bg-amber-600 px-3 py-1.5 font-medium text-white hover:bg-amber-700 disabled:opacity-60"
        >
          Set master
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-amber-300 bg-white px-3 py-1.5 hover:bg-amber-100"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
