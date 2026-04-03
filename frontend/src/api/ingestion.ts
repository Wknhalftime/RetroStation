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
      return apiUpload<UploadResult>(
        `/api/v1/stations/${stationId}/playlists/upload`,
        formData,
      );
    },
  });
}
