"""The cue editor's use cases: open a song's cues, save or reset a manual edit, and serve the
audio and waveform the editor draws (spec C2, C4, C5; PR 2).

A song opens on what the radio plays for it (autocue's row, else D23), so the editor never
shows markers the radio would not use. An edit is stored by audio hash, so twins share it.
Commands return nothing; queries write nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from backend.domain.enums import FileStatus
from backend.domain.library import AudioHash, LibraryFile
from backend.domain.streaming import (
    CueAnalysis,
    CueAudioUnreadableError,
    CueEditFileNotFoundError,
    CueEditRejectedError,
    CueFileNotEditableError,
    CueMarkers,
    PlayableFile,
    StreamTiming,
    edited_cues,
    no_cue_points,
)
from backend.playout.peaks import PeakReadError, Peaks
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.stream_cue_edits import StreamCueEditRepository
from backend.repositories.stream_cues import StreamCueRepository

NOT_FINGERPRINTED = "This file hasn't been fingerprinted yet, so its cues can't be edited."
NO_LENGTH = "This file has no known length."
SILENT_REFUSAL = "This song was measured silent, so the radio skips it; its cues can't be edited."


class CueAnalysisState(StrEnum):
    """Where a song's opening markers come from."""

    MEASURED = "measured"
    FAILED = "failed"
    WAITING = "waiting"
    SILENT = "silent"


@dataclass(frozen=True)
class CueEditorPorts:
    files: LibraryFileRepository
    cues: StreamCueRepository
    edits: StreamCueEditRepository


@dataclass(frozen=True)
class CueEditorView:
    file_id: UUID
    title: str | None
    artist: str | None
    format: str
    duration_ms: int
    analysis: CueAnalysisState
    automatic: CueMarkers
    manual: CueMarkers | None


@dataclass(frozen=True)
class Waveform:
    duration_ms: int
    per_second: int
    peaks: bytes


@dataclass(frozen=True)
class AudioSource:
    path: str
    format: str


type ReadPeaks = Callable[[str], Peaks]


@dataclass(frozen=True)
class _Editable:
    """A file the editor can open: its audio hash, the length it edits against, and the
    row autocue stored for the audio (None when there is none)."""

    file: LibraryFile
    audio_hash: AudioHash
    duration_ms: int
    row: CueAnalysis | None


def _present_file(ports: CueEditorPorts, file_id: UUID) -> LibraryFile:
    file = ports.files.get_by_id(file_id)
    if file is None or file.file_status != FileStatus.PRESENT:
        raise CueEditFileNotFoundError("This song is not in the library.")
    return file


def _file_on_disk(ports: CueEditorPorts, file_id: UUID) -> LibraryFile:
    file = _present_file(ports, file_id)
    if not Path(file.file_path).is_file():
        raise CueEditFileNotFoundError("This song's file is no longer on disk.")
    return file


def _editable(ports: CueEditorPorts, file_id: UUID) -> _Editable:
    file = _present_file(ports, file_id)
    if file.audio_hash is None:
        raise CueFileNotEditableError(NOT_FINGERPRINTED)
    row = ports.cues.get(file.audio_hash)
    tagged_ms = file.audio.duration_ms or 0
    # The longer of the tag and autocue's Stop: the opening markers are then always savable.
    duration_ms = max(tagged_ms, row.cues.cue_out_ms) if row is not None else tagged_ms
    if duration_ms <= 0:
        raise CueFileNotEditableError(NO_LENGTH)
    return _Editable(file, file.audio_hash, duration_ms, row)


def _analysis_state(row: CueAnalysis | None) -> CueAnalysisState:
    if row is None:
        return CueAnalysisState.WAITING
    if row.audio_silent:
        return CueAnalysisState.SILENT
    return CueAnalysisState.FAILED if row.analysis_failed else CueAnalysisState.MEASURED


def _opening_markers(target: _Editable, timing: StreamTiming) -> CueMarkers:
    """What the radio plays for this audio without an edit: autocue's row, else D23."""
    if target.row is not None:
        return CueMarkers.from_points(target.row.cues)
    return CueMarkers.from_points(no_cue_points(target.duration_ms, timing))


def read_cue_editor(ports: CueEditorPorts, file_id: UUID, timing: StreamTiming) -> CueEditorView:
    target = _editable(ports, file_id)
    return CueEditorView(
        file_id=target.file.id,
        title=target.file.audio.track_title,
        artist=target.file.audio.artist_name,
        format=target.file.format,
        duration_ms=target.duration_ms,
        analysis=_analysis_state(target.row),
        automatic=_opening_markers(target, timing),
        manual=ports.edits.get(target.audio_hash),
    )


def _refusal_for(target: _Editable, markers: CueMarkers, timing: StreamTiming) -> str | None:
    """Why the radio would not play these markers, or None when it would."""
    if target.row is not None and target.row.audio_silent:
        return SILENT_REFUSAL
    if markers.stop_ms > target.duration_ms:
        return f"Stop can't be past the end of the song ({target.duration_ms} ms)."
    stored = None if target.row is None else target.row.cues
    playable = PlayableFile(
        target.file.id,
        target.file.file_path,
        target.duration_ms,
        edited_cues(markers, stored),
    )
    if not playable.is_playable(timing):
        return (
            f"The fade needs at least {timing.margin_ms / 1000:g} s of the song before it: "
            "move Fade-out later or Start earlier."
        )
    return None


def save_cue_edit(
    ports: CueEditorPorts, file_id: UUID, markers: CueMarkers, timing: StreamTiming
) -> None:
    target = _editable(ports, file_id)
    reason = _refusal_for(target, markers, timing)
    if reason is not None:
        raise CueEditRejectedError(reason)
    ports.edits.save(target.audio_hash, markers)


def reset_cue_edit(ports: CueEditorPorts, file_id: UUID) -> None:
    target = _editable(ports, file_id)
    ports.edits.delete(target.audio_hash)


def _waveform_length_ms(file: LibraryFile, peaks: Peaks) -> int:
    """The tagged length, else the length the peaks themselves cover."""
    return file.audio.duration_ms or len(peaks.data) * 1000 // peaks.per_second


def read_waveform(ports: CueEditorPorts, file_id: UUID, read: ReadPeaks) -> Waveform:
    file = _file_on_disk(ports, file_id)
    try:
        peaks = read(file.file_path)
    except PeakReadError as error:
        raise CueAudioUnreadableError(str(error)) from error
    return Waveform(_waveform_length_ms(file, peaks), peaks.per_second, peaks.data)


def audio_path(ports: CueEditorPorts, file_id: UUID) -> AudioSource:
    file = _file_on_disk(ports, file_id)
    return AudioSource(file.file_path, file.format)
