import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Download } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { PlaylistEventTable } from "@/components/domain/playlists/PlaylistEventTable";
import { DatePicker } from "@/components/domain/playlists/DatePicker";
import { useStation } from "@/api/stations";
import { usePlaylists, useBroadcastDays, useExportM3u } from "@/api/playlists";
import { cn } from "@/lib/utils";
import type { PlaylistSummary } from "@/lib/schemas/playlists";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PlaylistViewer() {
  const { station_id } = useParams<{ station_id: string }>();

  const { data: station, isLoading: stationLoading } = useStation(station_id);
  const { data: playlists, isLoading: playlistsLoading } = usePlaylists(
    station_id,
  );

  const [selectedId, setSelectedId] = useState<string | undefined>(undefined);
  const [calendarMonth, setCalendarMonth] = useState(() => new Date());
  const [selectedDate, setSelectedDate] = useState<string | undefined>(
    undefined,
  );

  const exportMutation = useExportM3u();

  // Auto-select first playlist when list loads
  useEffect(() => {
    if (playlists && playlists.length > 0 && !selectedId) {
      setSelectedId(playlists[0].id);
    }
  }, [playlists, selectedId]);

  const selectedPlaylist: PlaylistSummary | undefined = playlists?.find(
    (p) => p.id === selectedId,
  );

  const { data: broadcastDays = [] } = useBroadcastDays(selectedId);

  // -------------------------------------------------------------------------
  // Loading state
  // -------------------------------------------------------------------------

  if (stationLoading || playlistsLoading) {
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
    : "Playlists";

  const handleExport = () => {
    if (!selectedId) return;
    exportMutation.mutate({
      playlistId: selectedId,
      formatName: station?.format_name ?? undefined,
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
        description="Broadcast playlists"
        actions={
          selectedId ? (
            <button
              type="button"
              onClick={handleExport}
              disabled={exportMutation.isPending}
              className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {exportMutation.isPending ? (
                <Spinner className="h-4 w-4" />
              ) : (
                <Download className="h-4 w-4" />
              )}
              Export M3U
            </button>
          ) : undefined
        }
      />

      {/* Main two-column layout */}
      {!playlists || playlists.length === 0 ? (
        <EmptyState
          title="No playlists yet"
          description="Upload a playlist CSV from the station dashboard to get started."
        />
      ) : (
        <div className="flex gap-6 items-start">
          {/* Left sidebar */}
          <aside className="w-80 shrink-0 space-y-4">
            {/* Playlist list */}
            <div className="rounded-lg border border-gray-200 bg-white shadow-sm overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100">
                <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                  Playlists
                </p>
              </div>
              <ul className="divide-y divide-gray-100 max-h-72 overflow-y-auto">
                {playlists.map((playlist) => (
                  <li key={playlist.id}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(playlist.id)}
                      className={cn(
                        "w-full px-4 py-3 text-left text-sm transition hover:bg-gray-50",
                        playlist.id === selectedId
                          ? "bg-indigo-50 font-semibold text-indigo-700"
                          : "text-gray-700",
                      )}
                    >
                      <span className="block truncate">{playlist.name}</span>
                      <span className="block text-xs text-gray-400 mt-0.5">
                        {playlist.event_count.toLocaleString()} events
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>

            {/* DatePicker */}
            {selectedId && (
              <DatePicker
                broadcastDays={broadcastDays}
                selectedDate={selectedDate}
                onSelect={setSelectedDate}
                month={calendarMonth}
                onMonthChange={setCalendarMonth}
              />
            )}
          </aside>

          {/* Right content */}
          <div className="flex-1 min-w-0">
            {selectedId ? (
              <>
                {selectedPlaylist && (
                  <p className="mb-3 text-sm font-medium text-gray-700">
                    {selectedPlaylist.name}
                  </p>
                )}
                <PlaylistEventTable playlistId={selectedId} />
              </>
            ) : (
              <EmptyState
                title="Select a playlist"
                description="Choose a playlist from the sidebar to view its events."
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
