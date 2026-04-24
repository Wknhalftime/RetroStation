import { Link } from "react-router-dom";
import { FolderCog, ScrollText } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { useSettings } from "@/api/settings";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Settings() {
  const { data: settings, isLoading, isError } = useSettings();

  const entries = settings ? Object.entries(settings) : [];

  return (
    <div className="space-y-6">
      <PageHeader title="Settings" description="Application configuration" />

      {/* Navigation links */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">
          Configuration
        </h2>
        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
          <Link
            to="/settings/paths"
            className="flex items-center gap-3 px-4 py-3 hover:bg-gray-50 transition-colors border-b border-gray-100"
          >
            <FolderCog className="h-5 w-5 shrink-0 text-indigo-500" />
            <div>
              <p className="text-sm font-medium text-gray-900">Path Configuration</p>
              <p className="text-xs text-gray-400">
                Local library path and Navidrome path prefix mapping
              </p>
            </div>
          </Link>
          <Link
            to="/settings/system-logs"
            className="flex items-center gap-3 px-4 py-3 hover:bg-gray-50 transition-colors"
          >
            <ScrollText className="h-5 w-5 shrink-0 text-indigo-500" />
            <div>
              <p className="text-sm font-medium text-gray-900">System Logs</p>
              <p className="text-xs text-gray-400">Operational log of background task activity</p>
            </div>
          </Link>
        </div>
      </section>

      {/* All settings table */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">
          All Settings
        </h2>

        {isLoading && (
          <div className="flex justify-center py-12">
            <Spinner className="h-6 w-6 text-indigo-500" />
          </div>
        )}

        {isError && (
          <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
            Failed to load settings. Please try again.
          </p>
        )}

        {!isLoading && !isError && entries.length === 0 && (
          <EmptyState
            title="No settings configured"
            description="Settings will appear here once values are set."
          />
        )}

        {!isLoading && !isError && entries.length > 0 && (
          <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 bg-gray-50 text-left">
                  <th className="px-4 py-2.5 font-semibold text-gray-600">Key</th>
                  <th className="px-4 py-2.5 font-semibold text-gray-600">Value</th>
                </tr>
              </thead>
              <tbody>
                {entries.map(([key, value], idx) => (
                  <tr
                    key={key}
                    className={idx < entries.length - 1 ? "border-b border-gray-100" : ""}
                  >
                    <td className="px-4 py-2.5 font-mono text-xs text-gray-700">{key}</td>
                    <td className="px-4 py-2.5 text-gray-600 break-all">{value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
