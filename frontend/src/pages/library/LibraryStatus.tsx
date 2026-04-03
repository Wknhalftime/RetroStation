import { Link } from "react-router-dom";
import { Database, AlertTriangle, Users } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { useLibraryStatus } from "@/api/library";
import { ScanLibraryButton } from "@/components/domain/library/ScanLibraryButton";

export function LibraryStatus() {
  const { data, isLoading, isError } = useLibraryStatus();

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
        Failed to load library status. Please try again.
      </p>
    );
  }

  const enrichedCount = data.by_enrichment["enriched"] ?? 0;
  const enrichedPct =
    data.total_files > 0
      ? Math.round((enrichedCount / data.total_files) * 100)
      : 0;

  const formatsSorted = Object.entries(data.by_format).sort(
    ([, a], [, b]) => b - a,
  );
  const enrichmentSorted = Object.entries(data.by_enrichment).sort(
    ([, a], [, b]) => b - a,
  );

  return (
    <div>
      <PageHeader
        title="Library"
        description="Overview of your music library files and enrichment status."
        actions={
          <>
            <ScanLibraryButton />
            <Link
              to="/library/artists"
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
            >
              <Users className="h-4 w-4" />
              Browse Artists
            </Link>
          </>
        }
      />

      {/* Stats cards */}
      <div className="mb-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {/* Total Files */}
        <div className="flex items-start gap-4 rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="rounded-lg bg-indigo-50 p-2">
            <Database className="h-5 w-5 text-indigo-600" />
          </div>
          <div>
            <p className="text-sm text-gray-500">Total Files</p>
            <p className="text-2xl font-semibold text-gray-900">
              {data.total_files.toLocaleString()}
            </p>
          </div>
        </div>

        {/* Enriched */}
        <div className="flex items-start gap-4 rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="rounded-lg bg-green-50 p-2">
            <Database className="h-5 w-5 text-green-600" />
          </div>
          <div>
            <p className="text-sm text-gray-500">Enriched</p>
            <p className="text-2xl font-semibold text-gray-900">
              {enrichedCount.toLocaleString()}
              <span className="ml-2 text-base font-normal text-gray-400">
                ({enrichedPct}%)
              </span>
            </p>
          </div>
        </div>

        {/* Quarantine — only shown when >0 */}
        {data.quarantine_count > 0 && (
          <div className="flex items-start gap-4 rounded-xl border border-amber-200 bg-amber-50 p-5 shadow-sm">
            <div className="rounded-lg bg-amber-100 p-2">
              <AlertTriangle className="h-5 w-5 text-amber-600" />
            </div>
            <div>
              <p className="text-sm text-amber-700">Quarantined</p>
              <p className="text-2xl font-semibold text-amber-900">
                {data.quarantine_count.toLocaleString()}
              </p>
            </div>
          </div>
        )}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Format breakdown */}
        <section className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-gray-500">
            Format Breakdown
          </h2>
          {formatsSorted.length === 0 ? (
            <p className="text-sm text-gray-400">No format data available.</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {formatsSorted.map(([format, count]) => (
                <span
                  key={format}
                  className="inline-flex items-center gap-1.5 rounded-full bg-gray-100 px-3 py-1 text-sm font-medium text-gray-700"
                >
                  <span className="font-semibold uppercase">{format}</span>
                  <span className="text-gray-400">{count.toLocaleString()}</span>
                </span>
              ))}
            </div>
          )}
        </section>

        {/* Enrichment status breakdown */}
        <section className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-gray-500">
            Enrichment Status
          </h2>
          {enrichmentSorted.length === 0 ? (
            <p className="text-sm text-gray-400">No enrichment data available.</p>
          ) : (
            <div className="space-y-2">
              {enrichmentSorted.map(([status, count]) => (
                <div key={status} className="flex items-center justify-between">
                  <span className="text-sm capitalize text-gray-600">
                    {status.replace(/_/g, " ")}
                  </span>
                  <span className="text-sm font-medium text-gray-900">
                    {count.toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
