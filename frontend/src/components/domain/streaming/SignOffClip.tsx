import type { ChangeEvent } from "react";
import { useState } from "react";
import { ApiError, ValidationError } from "@/api/client";
import { useStreamSettings, useUploadSignOff, useDeleteSignOff } from "@/api/streaming";

// ---------------------------------------------------------------------------
// Constants — PG3, M4: FLAC, MP3 or WAV, up to 25 MiB, 1 second to 5 minutes.
// ---------------------------------------------------------------------------

const MAX_CLIP_BYTES = 25 * 2 ** 20;

const RULE_TEXT =
  "FLAC, MP3 or WAV, up to 25 MB, 1 second to 5 minutes. A 5-minute WAV is too big: use MP3 " +
  "or FLAC for long clips.";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatSeconds(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(whole / 60);
  const secs = whole % 60;
  return `${minutes}:${secs.toString().padStart(2, "0")}`;
}

/** The server's reason for refusing a change to the clip, else ``fallback``. */
function describeRefusal(error: unknown, fallback: string): string {
  if (error instanceof ValidationError) {
    const messages = error.detail.map((entry) => entry.msg).filter((msg) => msg.length > 0);
    return messages.length > 0 ? messages.join("; ") : "Validation error";
  }
  if (error instanceof ApiError) return error.message;
  return fallback;
}

function describeLoadError(error: unknown): string {
  return error instanceof Error ? error.message : "The sign-off clip could not be loaded.";
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SignOffClip() {
  const { data, isError, error } = useStreamSettings();
  const upload = useUploadSignOff();
  const remove = useDeleteSignOff();
  const [refusal, setRefusal] = useState<string | null>(null);

  if (isError) {
    return (
      <div>
        <h3 className="mb-2 text-sm font-semibold text-gray-700">Sign-off clip</h3>
        <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
          {describeLoadError(error)}
        </p>
      </div>
    );
  }

  const signOff = data?.sign_off ?? null;

  function handleChoose(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;

    if (file.size > MAX_CLIP_BYTES) {
      setRefusal("The clip is larger than 25 MiB.");
      return;
    }

    setRefusal(null);
    upload.mutate(file, {
      onError: (uploadError) =>
        setRefusal(describeRefusal(uploadError, "The clip could not be uploaded.")),
    });
  }

  function handleRemove() {
    setRefusal(null);
    remove.mutate(undefined, {
      onError: (removeError) =>
        setRefusal(describeRefusal(removeError, "The clip could not be removed.")),
    });
  }

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-gray-700">Sign-off clip</h3>
      <p className="mb-3 text-xs text-gray-400">{RULE_TEXT}</p>

      {signOff ? (
        <div className="mb-3 flex items-center justify-between rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
          <p className="text-sm text-gray-700">
            {signOff.name} — {formatSeconds(signOff.seconds)} ({signOff.format.toUpperCase()})
          </p>
          <button
            type="button"
            onClick={handleRemove}
            disabled={remove.isPending}
            className="rounded-md border border-gray-300 px-3 py-1 text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-60"
          >
            Remove
          </button>
        </div>
      ) : (
        <p className="mb-3 text-sm text-gray-500">
          No sign-off clip is set; the stream ends after the last song.
        </p>
      )}

      {data?.sign_off_problem && (
        <p role="alert" className="mb-3 rounded-md bg-red-50 p-3 text-sm text-red-700">
          {data.sign_off_problem}
        </p>
      )}

      <label className="block text-sm text-gray-700">
        Upload a sign-off clip
        <input
          type="file"
          aria-label="Upload a sign-off clip"
          accept=".flac,.mp3,.wav,audio/flac,audio/mpeg,audio/wav"
          onChange={handleChoose}
          disabled={upload.isPending}
          className="mt-1 block text-sm text-gray-600"
        />
      </label>

      {upload.isPending && (
        <p role="status" className="mt-2 text-sm text-gray-500">
          Uploading…
        </p>
      )}

      {refusal && (
        <p role="alert" className="mt-2 rounded-md bg-red-50 p-3 text-sm text-red-700">
          {refusal}
        </p>
      )}
    </div>
  );
}
