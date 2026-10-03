import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { useStreamSettings } from "@/api/streaming";
import { MaxListenersSetting } from "@/components/domain/streaming/MaxListenersSetting";
import { SignOffClip } from "@/components/domain/streaming/SignOffClip";

// ---------------------------------------------------------------------------
// Streaming state — PG1: STREAM_ENABLED stays env-only, shown read-only here.
// H9: the page fails per section, never as a whole, so this section's own
// load failure does not keep the other sections from rendering.
// ---------------------------------------------------------------------------

function StreamingState() {
  const { data, isLoading, isError, error } = useStreamSettings();

  if (isError) {
    return (
      <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
        {error instanceof Error ? error.message : "The streaming state could not be loaded."}
      </p>
    );
  }

  if (isLoading || !data) {
    return (
      <div className="flex justify-center py-6">
        <Spinner className="h-5 w-5 text-indigo-500" />
      </div>
    );
  }

  return (
    <div>
      <p role="status" className="text-sm font-medium text-gray-900">
        Streaming is {data.streaming}.
      </p>
      <p className="mt-1 text-xs text-gray-400">
        Set by the <span className="font-mono">STREAM_ENABLED</span> environment variable; change it
        there and restart the server to turn streaming on or off.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Streaming() {
  return (
    <div className="space-y-6">
      <div>
        <Link
          to="/settings"
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          Settings
        </Link>
      </div>

      <PageHeader title="Streaming" description="Live listening settings" />

      <section className="overflow-hidden rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <StreamingState />
      </section>

      <section className="overflow-hidden rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <MaxListenersSetting />
      </section>

      <section className="overflow-hidden rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <SignOffClip />
      </section>
    </div>
  );
}
