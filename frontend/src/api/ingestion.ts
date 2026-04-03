import { useMutation } from "@tanstack/react-query";
import { apiUpload } from "@/api/client";

interface UploadPayload {
  file: File;
  stationId: string;
}

interface UploadResult {
  task_id: string;
  message: string;
}

export function useUploadPlaylist() {
  return useMutation<UploadResult, Error, UploadPayload>({
    mutationFn: ({ file, stationId }) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("station_id", stationId);
      return apiUpload<UploadResult>(
        `/api/v1/ingestion/playlists`,
        formData,
      );
    },
  });
}

// ---------------------------------------------------------------------------
// Batch upload — sequential uploads with progress callback
// ---------------------------------------------------------------------------

export interface BatchUploadPayload {
  files: File[];
  stationId: string;
  onProgress?: (completed: number, total: number, currentFile: string) => void;
}

export interface BatchUploadResult {
  total: number;
  succeeded: number;
  failed: number;
  errors: Array<{ filename: string; error: string }>;
}

export function useUploadPlaylists() {
  return useMutation<BatchUploadResult, Error, BatchUploadPayload>({
    mutationFn: async ({ files, stationId, onProgress }) => {
      const result: BatchUploadResult = {
        total: files.length,
        succeeded: 0,
        failed: 0,
        errors: [],
      };

      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        onProgress?.(i, files.length, file.name);

        try {
          const formData = new FormData();
          formData.append("file", file);
          formData.append("station_id", stationId);
          await apiUpload<UploadResult>(
            `/api/v1/ingestion/playlists`,
            formData,
          );
          result.succeeded++;
        } catch (err) {
          result.failed++;
          result.errors.push({
            filename: file.name,
            error: err instanceof Error ? err.message : "Unknown error",
          });
        }
      }

      onProgress?.(files.length, files.length, "");
      return result;
    },
  });
}
