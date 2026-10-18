import { useLocation, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import { useCueAudio, useCueEditor, useResetCues, useSaveCues, useWaveform } from "@/api/cues";
import type { CueEditor as CueEditorView } from "@/lib/schemas/cues";
import { CueEditorBody } from "@/components/domain/cues/CueEditorBody";
import { errorText } from "@/components/domain/cues/errorText";

// The cue editor page (/library/files/:file_id/cues): Lance sets Start, EOM, Fade-out and Stop
// for one file's audio on its waveform, previews the start and the end, and saves (R8). This page
// is the only part that fetches; the editor body, the waveform, the preview and the math are
// prop-driven.

const SILENT_NOTE = "Measured silent — the radio skips this song, so its cues can't be edited.";

/** The song's waveform, audio, save and reset, fetched only once the song could be opened. */
function SongCues({ fileId, editor }: { fileId: string; editor: CueEditorView }) {
  const waveform = useWaveform(fileId);
  const audio = useCueAudio(fileId);
  const save = useSaveCues(fileId);
  const reset = useResetCues(fileId);
  return (
    <CueEditorBody
      editor={editor}
      waveform={waveform}
      audio={audio}
      save={save}
      reset={reset}
      lockFade={false}
      silentNote={SILENT_NOTE}
    />
  );
}

export function CueEditor() {
  const { file_id: fileId = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const editor = useCueEditor(fileId);

  let body;
  if (editor.data) {
    // A failed background refetch keeps the data: show the editor (and its unsaved edits) as is.
    body = (
      <>
        {editor.isError && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
            Couldn't refresh this song's cues. {errorText(editor.error)}
          </p>
        )}
        <SongCues key={fileId} fileId={fileId} editor={editor.data} />
      </>
    );
  } else if (editor.isError) {
    body = (
      <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
        {errorText(editor.error)}
      </p>
    );
  } else {
    body = (
      <div className="flex justify-center py-12">
        <Spinner className="h-6 w-6 text-indigo-500" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <button
          type="button"
          // "default" is the first entry of this session: nothing in the app to go back to.
          onClick={() => (location.key === "default" ? navigate("/library") : navigate(-1))}
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to the song
        </button>
      </div>
      {body}
    </div>
  );
}
