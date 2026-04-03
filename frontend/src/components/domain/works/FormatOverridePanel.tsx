import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { FormatOverrideInfo, RecordingDetail } from "@/lib/schemas/works";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface FormatOverridePanelProps {
  overrides: FormatOverrideInfo[];
  recordings: RecordingDetail[];
  onAdd: (data: {
    format_name: string;
    preferred_file_id: string;
    notes: string | null;
  }) => void;
  onDelete: (oid: string) => void;
  isAdding?: boolean;
  isDeleting?: boolean;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function FormatOverridePanel({
  overrides,
  recordings,
  onAdd,
  onDelete,
  isAdding = false,
  isDeleting = false,
}: FormatOverridePanelProps) {
  const [showForm, setShowForm] = useState(false);
  const [formatName, setFormatName] = useState("");
  const [preferredFileId, setPreferredFileId] = useState("");
  const [notes, setNotes] = useState("");

  // Flatten all files across recordings for the dropdown
  const allFiles = recordings.flatMap((rec) =>
    rec.files.map((f) => ({ ...f, recordingTitle: rec.title })),
  );

  function handleSave() {
    if (!formatName.trim() || !preferredFileId) return;
    onAdd({
      format_name: formatName.trim(),
      preferred_file_id: preferredFileId,
      notes: notes.trim() || null,
    });
    // Reset form
    setFormatName("");
    setPreferredFileId("");
    setNotes("");
    setShowForm(false);
  }

  function handleCancel() {
    setFormatName("");
    setPreferredFileId("");
    setNotes("");
    setShowForm(false);
  }

  return (
    <div className="rounded-xl border border-gray-200 bg-white shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3">
        <h3 className="text-sm font-semibold text-gray-700">Format Overrides</h3>
        {!showForm && (
          <button
            type="button"
            onClick={() => setShowForm(true)}
            className="inline-flex items-center gap-1 rounded-md bg-indigo-50 px-2.5 py-1 text-xs font-medium text-indigo-600 hover:bg-indigo-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
          >
            <Plus className="h-3.5 w-3.5" />
            Add Override
          </button>
        )}
      </div>

      {/* Add form */}
      {showForm && (
        <div className="border-b border-gray-100 bg-gray-50 px-4 py-3 space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {/* Format name */}
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">
                Format Name
              </label>
              <input
                type="text"
                value={formatName}
                onChange={(e) => setFormatName(e.target.value)}
                placeholder="e.g. FLAC, MP3"
                className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-400"
              />
            </div>

            {/* File select */}
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">
                Preferred File
              </label>
              <select
                value={preferredFileId}
                onChange={(e) => setPreferredFileId(e.target.value)}
                className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-400"
              >
                <option value="">Select a file...</option>
                {allFiles.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.recordingTitle} — {f.format.toUpperCase()}{" "}
                    {f.bitrate != null ? `(${f.bitrate} kbps)` : ""}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Notes */}
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-600">
              Notes (optional)
            </label>
            <input
              type="text"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Reason for override..."
              className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleSave}
              disabled={!formatName.trim() || !preferredFileId || isAdding}
              className={cn(
                "rounded-md px-3 py-1.5 text-xs font-medium text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400",
                formatName.trim() && preferredFileId && !isAdding
                  ? "bg-indigo-600 hover:bg-indigo-700"
                  : "cursor-not-allowed bg-indigo-300",
              )}
            >
              {isAdding ? "Saving..." : "Save"}
            </button>
            <button
              type="button"
              onClick={handleCancel}
              className="rounded-md px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Overrides list */}
      <div className="px-4 py-3">
        {overrides.length === 0 ? (
          <p className="text-sm text-gray-400">
            No format overrides configured. Files will use the global master selection.
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {overrides.map((override) => (
              <div
                key={override.id}
                className="inline-flex items-center gap-1.5 rounded-full border border-gray-200 bg-gray-100 px-3 py-1 text-xs font-medium text-gray-700"
              >
                <span className="font-mono uppercase text-indigo-600">
                  {override.format_name}
                </span>
                {override.notes && (
                  <span className="text-gray-500">— {override.notes}</span>
                )}
                <button
                  type="button"
                  onClick={() => onDelete(override.id)}
                  disabled={isDeleting}
                  aria-label={`Delete ${override.format_name} override`}
                  className="ml-0.5 rounded-full p-0.5 text-gray-400 hover:bg-gray-200 hover:text-red-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400 disabled:opacity-50"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
