import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { MatchStatusBadge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { useStationEvents } from "@/api/stations";
import { formatDateTime } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface StationEventTableProps {
  stationId: string;
  date: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function StationEventTable({ stationId, date }: StationEventTableProps) {
  const [offset, setOffset] = useState(0);

  // Reset to first page when date changes
  useEffect(() => {
    setOffset(0);
  }, [date]);

  const { data, isLoading, isError } = useStationEvents(
    stationId,
    date,
    PAGE_SIZE,
    offset,
  );

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
        Failed to load broadcast events.
      </p>
    );
  }

  if (data.items.length === 0 && offset === 0) {
    return (
      <EmptyState
        title="No events"
        description="No broadcast events recorded for this date."
      />
    );
  }

  // -------------------------------------------------------------------------
  // Main render
  // -------------------------------------------------------------------------

  return (
    <div className="flex flex-col gap-3">
      {/* Total count header */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500">
          {data.total.toLocaleString()} events total
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
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Time
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Playlist
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Artist
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Title
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Status
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Tier
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((event) => (
              <tr key={event.id} className="hover:bg-gray-50">
                <td className="whitespace-nowrap px-4 py-3 text-gray-600">
                  {formatDateTime(event.played_at)}
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-gray-500">
                  {event.playlist_name}
                </td>
                <td className="px-4 py-3 text-gray-900">
                  {event.artist_name}
                </td>
                <td className="px-4 py-3 text-gray-900">{event.title}</td>
                <td className="px-4 py-3">
                  <MatchStatusBadge status={event.match_status} />
                </td>
                <td className="px-4 py-3 text-gray-500">
                  {event.match_tier ?? (
                    <span className="text-gray-300">—</span>
                  )}
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
