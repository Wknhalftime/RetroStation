import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Plus, Trash2, Radio, MapPin, Music, ListMusic } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { Spinner } from "@/components/ui/Spinner";
import { StationForm } from "@/components/domain/stations/StationForm";
import {
  useStations,
  useCreateStation,
  useDeleteStation,
} from "@/api/stations";
import type { StationCreate } from "@/lib/schemas/stations";

export function StationList() {
  const [showCreate, setShowCreate] = useState(false);
  // Tags each submission with the modal session it belongs to. If the user
  // reopens the modal before a slow mutation resolves, the stale onSuccess
  // won't close the freshly opened modal.
  const modalSessionRef = useRef(0);

  const { data: stations, isLoading, isError } = useStations();
  const createMutation = useCreateStation();
  const deleteMutation = useDeleteStation();

  const openCreateModal = () => {
    modalSessionRef.current += 1;
    setShowCreate(true);
  };

  const handleCreate = (data: StationCreate) => {
    const currentSession = modalSessionRef.current;
    createMutation.mutate(data, {
      onSuccess: () => {
        if (currentSession === modalSessionRef.current) {
          setShowCreate(false);
        }
      },
    });
  };

  const handleDelete = (id: string, e: React.MouseEvent) => {
    e.preventDefault(); // prevent card link navigation
    if (
      window.confirm(
        "Delete this station? This permanently removes its playlists, broadcast calendar, and all play event history. This cannot be undone.",
      )
    ) {
      deleteMutation.mutate(id);
    }
  };

  return (
    <div>
      <PageHeader
        title="Stations"
        description="Manage your radio stations and their playlists."
        actions={
          <button
            type="button"
            onClick={openCreateModal}
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            <Plus className="h-4 w-4" />
            Add Station
          </button>
        }
      />

      {/* Loading */}
      {isLoading && (
        <div className="flex justify-center py-16">
          <Spinner className="h-8 w-8 text-indigo-500" />
        </div>
      )}

      {/* Error */}
      {isError && (
        <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
          Failed to load stations. Please try again.
        </p>
      )}

      {/* Empty state */}
      {!isLoading && !isError && stations?.length === 0 && (
        <EmptyState
          title="No stations yet"
          description="Add your first station to start importing playlists."
          actions={
            <button
              type="button"
              onClick={openCreateModal}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
            >
              <Plus className="h-4 w-4" />
              Add Station
            </button>
          }
        />
      )}

      {/* Station grid */}
      {stations && stations.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {stations.map((station) => (
            <Link
              key={station.id}
              to={`/stations/${station.id}`}
              className="group relative flex flex-col rounded-xl border border-gray-200 bg-white p-5 shadow-sm transition hover:border-indigo-300 hover:shadow-md"
            >
              {/* Delete button */}
              <button
                type="button"
                onClick={(e) => handleDelete(station.id, e)}
                className="absolute right-3 top-3 rounded p-1 text-gray-300 opacity-0 transition hover:bg-red-50 hover:text-red-500 group-hover:opacity-100"
                aria-label={`Delete ${station.call_letters}`}
              >
                <Trash2 className="h-4 w-4" />
              </button>

              {/* Call letters */}
              <div className="mb-2 flex items-center gap-2">
                <Radio className="h-5 w-5 text-indigo-400" />
                <span className="text-lg font-bold text-gray-900">
                  {station.call_letters}
                </span>
              </div>

              {/* Name */}
              {station.name && (
                <p className="mb-3 text-sm font-medium text-gray-700">
                  {station.name}
                </p>
              )}

              {/* Meta rows */}
              <div className="mt-auto space-y-1 text-xs text-gray-500">
                {station.city && (
                  <div className="flex items-center gap-1.5">
                    <MapPin className="h-3.5 w-3.5" />
                    {station.city}
                  </div>
                )}
                {station.format_name && (
                  <div className="flex items-center gap-1.5">
                    <Music className="h-3.5 w-3.5" />
                    {station.format_name}
                  </div>
                )}
                <div className="flex items-center gap-1.5">
                  <ListMusic className="h-3.5 w-3.5" />
                  {station.playlist_count}{" "}
                  {station.playlist_count === 1 ? "playlist" : "playlists"}
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}

      {/* Create modal */}
      <Modal
        open={showCreate}
        onClose={() => setShowCreate(false)}
        title="Add Station"
      >
        {showCreate && (
          <StationForm
            onSubmit={(data) => handleCreate(data as StationCreate)}
            onCancel={() => setShowCreate(false)}
            isPending={createMutation.isPending}
          />
        )}
      </Modal>
    </div>
  );
}
