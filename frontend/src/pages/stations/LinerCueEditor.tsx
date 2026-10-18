import { useLocation, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import {
  useLinerCueAudio,
  useLinerCues,
  useLinerWaveform,
  useResetLinerCues,
  useSaveLinerCues,
} from "@/api/liners";
import type { CueEditor as CueEditorView } from "@/lib/schemas/cues";
import { CueEditorBody } from "@/components/domain/cues/CueEditorBody";
import { errorText } from "@/components/domain/cues/errorText";

// The cue editor opened on a station liner (/stations/:station_id/liners/:liner_id/cues, LR11):
// the song editor's body with Fade-out hidden and pinned to Stop, since a liner has no fade.
// This page is the only part that fetches.

const SILENT_NOTE = "Measured silent — the radio skips this liner, so its cues can't be edited.";

interface LinerCuesProps {
  stationId: string;
  linerId: string;
  editor: CueEditorView;
}

/** The liner's waveform, audio, save and reset, fetched only once the liner could be opened. */
function LinerCues({ stationId, linerId, editor }: LinerCuesProps) {
  const waveform = useLinerWaveform(stationId, linerId);
  const audio = useLinerCueAudio(stationId, linerId);
  const save = useSaveLinerCues(stationId, linerId);
  const reset = useResetLinerCues(stationId, linerId);
  return (
    <CueEditorBody
      editor={editor}
      waveform={waveform}
      audio={audio}
      save={save}
      reset={reset}
      lockFade
      silentNote={SILENT_NOTE}
    />
  );
}

export function LinerCueEditor() {
  const { station_id: stationId = "", liner_id: linerId = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const editor = useLinerCues(stationId, linerId);

  let body;
  if (editor.data) {
    // A failed background refetch keeps the data: show the editor (and its unsaved edits) as is.
    body = (
      <>
        {editor.isError && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
            Couldn't refresh this liner's cues. {errorText(editor.error)}
          </p>
        )}
        <LinerCues key={linerId} stationId={stationId} linerId={linerId} editor={editor.data} />
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
          // "default" is the first entry of this session: open the station's Liners tab instead.
          onClick={() =>
            location.key === "default"
              ? navigate(`/stations/${stationId}?tab=liners`)
              : navigate(-1)
          }
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to the liners
        </button>
      </div>
      {body}
    </div>
  );
}
