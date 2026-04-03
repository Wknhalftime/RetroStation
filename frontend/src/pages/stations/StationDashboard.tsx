import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Pencil,
  Upload,
  Music,
  CalendarDays,
  ListMusic,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Modal } from "@/components/ui/Modal";
import { Spinner } from "@/components/ui/Spinner";
import { StationForm } from "@/components/domain/stations/StationForm";
import { useStation, useUpdateStation } from "@/api/stations";
import { useUploadPlaylist } from "@/api/ingestion";
import { formatDate } from "@/lib/utils";
import type { StationUpdate } from "@/lib/schemas/stations";

type UploadStatus =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "success"; message: string }
  | { kind: "error"; message: string };

export function StationDashboard() {
  const { station_id } = useParams<{ station_id: string }>();
  const [showEdit, setShowEdit] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<UploadStatus>({
    kind: "idle",
  });
  const fileInputRef = useRef<HTMLInputElement>(null);

  const { data: station, isLoading, isError } = useStation(station_id);
  const updateMutation = useUpdateStation(station_id ?? "");
  const uploadMutation = useUploadPlaylist();

  // -------------------------------------------------------------------------
  // Edit handler
  // -------------------------------------------------------------------------

  const handleUpdate = (data: StationUpdate) => {
    if (!station_id) return;
    updateMutation.mutate(data, {
      onSuccess: () => setShowEdit(false),
    });
  };

  // -------------------------------------------------------------------------
  // Upload handler
  // -------------------------------------------------------------------------

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !station_id) return;

    setUploadStatus({ kind: "pending" });

    uploadMutation.mutate(
      { file, stationId: station_id },
      {
        onSuccess: (result) => {
          setUploadStatus({
            kind: "success",
            message: result.message ?? "Upload started successfully.",
          });
          // Reset file input so the same file can be re-uploaded
          if (fileInputRef.current) fileInputRef.current.value = "";
        },
        onError: (err) => {
          setUploadStatus({
            kind: "error",
            message: err.message ?? "Upload failed.",
          });
          if (fileInputRef.current) fileInputRef.current.value = "";
        },
      },
    );
  };

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

  if (isError || !station) {
    return (
      <div className="space-y-4">
        <Link
          to="/stations"
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Stations
        </Link>
        <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
          Station not found or failed to load.
        </p>
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Main render
  // -------------------------------------------------------------------------

  const displayTitle = station.name
    ? `${station.call_letters} — ${station.name}`
    : station.call_letters;

  return (
    <div className="space-y-6">
      {/* Back link */}
      <Link
        to="/stations"
        className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Stations
      </Link>

      {/* Page header */}
      <PageHeader
        title={displayTitle}
        description={station.city ?? undefined}
        actions={
          <button
            type="button"
            onClick={() => setShowEdit(true)}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            <Pencil className="h-4 w-4" />
            Edit
          </button>
        }
      />

      {/* Info cards */}
      <div className="grid gap-4 sm:grid-cols-3">
        {/* Format */}
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-gray-500">
            <Music className="h-4 w-4" />
            Format
          </div>
          <p className="text-base font-semibold text-gray-900">
            {station.format_name ?? <span className="text-gray-400">—</span>}
          </p>
        </div>

        {/* Created */}
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-gray-500">
            <CalendarDays className="h-4 w-4" />
            Created
          </div>
          <p className="text-base font-semibold text-gray-900">
            {formatDate(station.created_at)}
          </p>
        </div>

        {/* Playlists link */}
        <Link
          to={`/stations/${station.id}/playlists`}
          className="flex flex-col rounded-xl border border-gray-200 bg-white p-5 shadow-sm transition hover:border-indigo-300 hover:shadow-md"
        >
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-gray-500">
            <ListMusic className="h-4 w-4" />
            Playlists
          </div>
          <p className="text-base font-semibold text-indigo-600">
            View Playlists
          </p>
        </Link>
      </div>

      {/* CSV upload */}
      <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
        <h2 className="mb-1 text-sm font-semibold text-gray-900">
          Upload Playlist CSV
        </h2>
        <p className="mb-4 text-xs text-gray-500">
          Select a CSV file exported from your broadcast system. Processing
          begins immediately after upload.
        </p>

        <label
          className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
          aria-label="Choose CSV file to upload"
        >
          <Upload className="h-4 w-4" />
          {uploadStatus.kind === "pending" ? (
            <>
              <Spinner className="h-4 w-4" />
              Uploading…
            </>
          ) : (
            "Choose File"
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,text/csv"
            className="sr-only"
            onChange={handleFileChange}
            disabled={uploadStatus.kind === "pending"}
          />
        </label>

        {/* Upload feedback */}
        {uploadStatus.kind === "success" && (
          <div className="mt-3 flex items-center gap-2 rounded-md bg-green-50 px-3 py-2 text-sm text-green-700">
            <CheckCircle2 className="h-4 w-4 shrink-0" />
            {uploadStatus.message}
          </div>
        )}
        {uploadStatus.kind === "error" && (
          <div className="mt-3 flex items-center gap-2 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {uploadStatus.message}
          </div>
        )}
      </div>

      {/* Edit modal */}
      <Modal
        open={showEdit}
        onClose={() => setShowEdit(false)}
        title="Edit Station"
      >
        <StationForm
          initial={{
            call_letters: station.call_letters,
            name: station.name,
            city: station.city,
            format_name: station.format_name,
          }}
          onSubmit={(data) => handleUpdate(data as StationUpdate)}
          onCancel={() => setShowEdit(false)}
          isPending={updateMutation.isPending}
        />
      </Modal>
    </div>
  );
}
