import { useState } from "react";
import { cn } from "@/lib/utils";
import { Spinner } from "@/components/ui/Spinner";
import type { StationCreate, StationUpdate } from "@/lib/schemas/stations";

// The form works for both create and update.
// `initial` is provided when editing an existing station.
export interface StationFormInitial {
  call_letters: string;
  name?: string | null;
  city?: string | null;
  format_name?: string | null;
}

interface StationFormProps {
  initial?: StationFormInitial;
  onSubmit: (data: StationCreate | StationUpdate) => void;
  onCancel: () => void;
  isPending: boolean;
}

export function StationForm({
  initial,
  onSubmit,
  onCancel,
  isPending,
}: StationFormProps) {
  const [callLetters, setCallLetters] = useState(initial?.call_letters ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const [city, setCity] = useState(initial?.city ?? "");
  const [formatName, setFormatName] = useState(initial?.format_name ?? "");

  const isEdit = Boolean(initial);
  const formatChanged =
    isEdit && formatName !== (initial?.format_name ?? "");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const payload = {
      call_letters: callLetters.trim(),
      name: name.trim() || null,
      city: city.trim() || null,
      format_name: formatName.trim() || null,
    };
    onSubmit(payload);
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {/* call_letters */}
      <div>
        <label
          htmlFor="sf-call-letters"
          className="block text-sm font-medium text-gray-700"
        >
          Call Letters <span className="text-red-500">*</span>
        </label>
        <input
          id="sf-call-letters"
          type="text"
          required
          value={callLetters}
          onChange={(e) => setCallLetters(e.target.value)}
          placeholder="e.g. WKRP"
          className={cn(
            "mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm",
            "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500",
          )}
        />
      </div>

      {/* name */}
      <div>
        <label
          htmlFor="sf-name"
          className="block text-sm font-medium text-gray-700"
        >
          Station Name
        </label>
        <input
          id="sf-name"
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Classic Rock 98.7"
          className={cn(
            "mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm",
            "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500",
          )}
        />
      </div>

      {/* city */}
      <div>
        <label
          htmlFor="sf-city"
          className="block text-sm font-medium text-gray-700"
        >
          City
        </label>
        <input
          id="sf-city"
          type="text"
          value={city}
          onChange={(e) => setCity(e.target.value)}
          placeholder="e.g. Cincinnati"
          className={cn(
            "mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm",
            "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500",
          )}
        />
      </div>

      {/* format_name */}
      <div>
        <label
          htmlFor="sf-format"
          className="block text-sm font-medium text-gray-700"
        >
          Format
        </label>
        <input
          id="sf-format"
          type="text"
          value={formatName}
          onChange={(e) => setFormatName(e.target.value)}
          placeholder="e.g. Classic Rock"
          className={cn(
            "mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm",
            "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500",
          )}
        />
        {formatChanged && (
          <p className="mt-1 text-xs text-yellow-600">
            Warning: changing the format may affect playlist categorization.
          </p>
        )}
      </div>

      {/* Actions */}
      <div className="flex justify-end gap-2 pt-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={isPending}
          className="rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={isPending || !callLetters.trim()}
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          {isPending && <Spinner className="h-4 w-4" />}
          {isEdit ? "Save Changes" : "Create Station"}
        </button>
      </div>
    </form>
  );
}
