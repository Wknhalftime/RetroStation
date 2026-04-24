import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Download } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { StationEventTable } from "@/components/domain/stations/StationEventTable";
import { DatePicker } from "@/components/domain/playlists/DatePicker";
import { useStation, useStationBroadcastDays, useExportStationM3u } from "@/api/stations";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PlaylistViewer() {
  const { station_id } = useParams<{ station_id: string }>();

  const { data: station, isLoading: stationLoading } = useStation(station_id);
  const { data: broadcastDays = [] } = useStationBroadcastDays(station_id);

  const [calendarMonth, setCalendarMonth] = useState(() => new Date());
  const [selectedDate, setSelectedDate] = useState<string | undefined>(undefined);

  const exportMutation = useExportStationM3u();

  // -------------------------------------------------------------------------
  // Loading state
  // -------------------------------------------------------------------------

  if (stationLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Derived values
  // -------------------------------------------------------------------------

  const displayTitle = station
    ? station.name
      ? `${station.call_letters} — ${station.name}`
      : station.call_letters
    : "Broadcasts";

  const handleExport = () => {
    if (!station_id || !selectedDate || !station) return;
    exportMutation.mutate({
      stationId: station_id,
      date: selectedDate,
      callLetters: station.call_letters,
    });
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Back link */}
      <Link
        to={station_id ? `/stations/${station_id}` : "/stations"}
        className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Station
      </Link>

      {/* Page header */}
      <PageHeader
        title={displayTitle}
        description="Broadcast calendar"
        actions={
          <button
            type="button"
            onClick={handleExport}
            disabled={!selectedDate || exportMutation.isPending}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {exportMutation.isPending ? (
              <Spinner className="h-4 w-4" />
            ) : (
              <Download className="h-4 w-4" />
            )}
            {selectedDate ? `Export M3U for ${selectedDate}` : "Export M3U"}
          </button>
        }
      />

      {/* Main two-column layout */}
      <div className="flex gap-6 items-start">
        {/* Left sidebar — calendar only */}
        <aside className="w-80 shrink-0">
          <DatePicker
            broadcastDays={broadcastDays}
            selectedDate={selectedDate}
            onSelect={setSelectedDate}
            month={calendarMonth}
            onMonthChange={setCalendarMonth}
          />
        </aside>

        {/* Right content */}
        <div className="flex-1 min-w-0">
          {selectedDate && station_id ? (
            <>
              <p className="mb-3 text-sm font-medium text-gray-700">{selectedDate}</p>
              <StationEventTable stationId={station_id} date={selectedDate} />
            </>
          ) : (
            <EmptyState
              title="Select a date"
              description="Select a date from the calendar to view broadcasts."
            />
          )}
        </div>
      </div>
    </div>
  );
}
