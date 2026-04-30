import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { useMissingMatchesReport } from "@/api/stations";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface MissingMatchesReportProps {
  stationId: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MissingMatchesReport({ stationId }: MissingMatchesReportProps) {
  const [offset, setOffset] = useState(0);

  // Reset to first page if stationId ever changes (defensive; unlikely in use)
  useEffect(() => {
    setOffset(0);
  }, [stationId]);

  const { data, isLoading, isError } = useMissingMatchesReport(stationId, PAGE_SIZE, offset);

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 1;

  const handlePrev = () => setOffset((o) => Math.max(0, o - PAGE_SIZE));
  const handleNext = () =>
    setOffset((o) => (o + PAGE_SIZE < (data?.total ?? 0) ? o + PAGE_SIZE : o));

  // -------------------------------------------------------------------------
  // Loading / error states
  // -------------------------------------------------------------------------

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
        Failed to load the Missing Matches report.
      </p>
    );
  }

  if (data.items.length === 0 && offset === 0) {
    return (
      <EmptyState
        title="No missing matches"
        description="All track identities for this station have been resolved."
      />
    );
  }

  // -------------------------------------------------------------------------
  // Main render
  // -------------------------------------------------------------------------

  return (
    <div className="flex flex-col gap-3">
      {/* Summary header */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500">
          {data.total.toLocaleString()} unresolved{" "}
          {data.total === 1 ? "identity" : "identities"}
        </p>
        <p className="text-sm text-gray-400">
          Page {page} of {totalPages}
        </p>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Artist</th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Track Title</th>
              <th className="px-4 py-3 text-right font-medium text-gray-500">Total Plays</th>
              <th className="px-4 py-3 text-right font-medium text-gray-500">Impact %</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((item) => (
              <tr key={item.identity_id} className="hover:bg-gray-50">
                <td className="px-4 py-3 text-gray-900">{item.artist_name}</td>
                <td className="px-4 py-3 text-gray-900">{item.track_title}</td>
                <td className="px-4 py-3 text-right tabular-nums text-gray-700">
                  {item.play_count.toLocaleString()}
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-gray-700">
                  {item.impact_pct.toFixed(2)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination controls */}
      <div className="flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={handlePrev}
          disabled={offset === 0}
          className="inline-flex items-center gap-1 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          aria-label="Previous page"
        >
          <ChevronLeft className="h-4 w-4" />
          Previous
        </button>
        <button
          type="button"
          onClick={handleNext}
          disabled={offset + PAGE_SIZE >= data.total}
          className="inline-flex items-center gap-1 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          aria-label="Next page"
        >
          Next
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
