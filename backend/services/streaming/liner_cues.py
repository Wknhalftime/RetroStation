"""The liner cue editor's use cases: open a liner's cues, save or reset its manual edit, and
serve the waveform the editor draws (station liners LR4, LR11; PR L3).

A liner opens on what the radio plays for it (``liner_points``: autocue's row with no fades,
or the whole file before analysis), with Fade-out pinned to Stop. An edit is stored on the
liner's own row, never by audio hash, so the same audio in another station keeps its own.
It reuses the song editor's ``CueEditorView``, ``Waveform``, analysis state and clock text,
and the liner store's ownership check and file path. Save refuses what ``liner_points`` would
not play (a silent liner). Commands return nothing; queries write nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from backend.domain.liners import LinerNotFoundError, StationLiner, liner_markers, liner_points
from backend.domain.streaming import (
    CueAnalysis,
    CueAudioUnreadableError,
    CueEditRejectedError,
    CueMarkers,
)
from backend.playout.peaks import PeakReadError
from backend.repositories.station_liners import StationLinerRepository
from backend.repositories.stream_cues import StreamCueRepository
from backend.services.streaming.cue_editor import (
    CueEditorView,
    ReadPeaks,
    Waveform,
    analysis_state,
    clock_text,
)
from backend.services.streaming.liners import liner_file, owned_liner

SILENT_REFUSAL = "This liner was measured silent, so the radio skips it; its cues can't be edited."


@dataclass(frozen=True)
class LinerCuePorts:
    """What the liner cue editor reads and writes; ``folder`` is the liners root and
    ``commit`` makes a write permanent."""

    liners: StationLinerRepository
    cues: StreamCueRepository
    folder: Path
    commit: Callable[[], None]


def _opening_markers(liner: StationLiner, row: CueAnalysis | None) -> CueMarkers:
    """What the radio plays for this liner without an edit, held within its length so the
    markers the editor opens on can always be saved; a silent liner opens on the whole file."""
    duration_ms = liner.duration_ms
    points = liner_points(duration_ms, None, row)
    if points is None:
        return liner_markers(0, duration_ms, duration_ms)
    stop_ms = min(points.cue_out_ms, duration_ms)
    start_ms = min(points.cue_in_ms, stop_ms - 1)
    eom_ms = min(max(points.cue_out_ms - points.start_next_ms, start_ms + 1), stop_ms)
    return liner_markers(start_ms, eom_ms, stop_ms)


def read_liner_cues(ports: LinerCuePorts, station_id: UUID, liner_id: UUID) -> CueEditorView:
    liner = owned_liner(ports.liners, station_id, liner_id)
    row = ports.cues.get(liner.audio_hash)
    return CueEditorView(
        file_id=liner.id,
        title=liner.name,
        artist=None,
        format=liner.format.value,
        duration_ms=liner.duration_ms,
        analysis=analysis_state(row),
        automatic=_opening_markers(liner, row),
        manual=liner.markers,
    )


def save_liner_cues(
    ports: LinerCuePorts, station_id: UUID, liner_id: UUID, start_ms: int, eom_ms: int, stop_ms: int
) -> None:
    liner = owned_liner(ports.liners, station_id, liner_id)
    row = ports.cues.get(liner.audio_hash)
    if liner_points(liner.duration_ms, None, row) is None:
        raise CueEditRejectedError(SILENT_REFUSAL)
    if stop_ms > liner.duration_ms:
        end = clock_text(liner.duration_ms)
        raise CueEditRejectedError(f"Stop can't be past the end of the liner ({end}).")
    ports.liners.save_markers(liner.id, liner_markers(start_ms, eom_ms, stop_ms))
    ports.commit()


def reset_liner_cues(ports: LinerCuePorts, station_id: UUID, liner_id: UUID) -> None:
    liner = owned_liner(ports.liners, station_id, liner_id)
    ports.liners.save_markers(liner.id, None)
    ports.commit()


def read_liner_waveform(
    ports: LinerCuePorts, station_id: UUID, liner_id: UUID, read: ReadPeaks
) -> Waveform:
    liner = owned_liner(ports.liners, station_id, liner_id)
    path = liner_file(ports.folder, liner)
    if not path.is_file():
        raise LinerNotFoundError(f"The file of liner {liner_id} is no longer on disk.")
    try:
        peaks = read(str(path))
    except PeakReadError as error:
        raise CueAudioUnreadableError(str(error)) from error
    return Waveform(liner.duration_ms, peaks.per_second, peaks.data)
