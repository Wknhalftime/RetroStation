"""The cue editor's response shapes, shared by the song editor (``/library``) and the liner
editor (``/stations``) so both answer with one definition."""

from __future__ import annotations

import base64
from uuid import UUID

from pydantic import BaseModel, Field

from backend.domain.streaming import CueMarkers
from backend.services.streaming.cue_editor import CueAnalysisState, CueEditorView, Waveform


class MarkersModel(BaseModel):
    """The four markers, whole milliseconds from the start of the file (strict, >= 0)."""

    start_ms: int = Field(strict=True, ge=0)
    eom_ms: int = Field(strict=True, ge=0)
    fade_at_ms: int = Field(strict=True, ge=0)
    stop_ms: int = Field(strict=True, ge=0)


class CueEditorOut(BaseModel):
    """Response body for GET .../cues: the editor's opening view."""

    file_id: UUID
    title: str | None
    artist: str | None
    format: str
    duration_ms: int
    analysis: CueAnalysisState
    automatic: MarkersModel
    manual: MarkersModel | None


class WaveformOut(BaseModel):
    """Response body for GET .../waveform: ``peaks`` is base64 of the bytes."""

    duration_ms: int
    per_second: int
    peaks: str


def markers_out(markers: CueMarkers) -> MarkersModel:
    return MarkersModel(
        start_ms=markers.start_ms,
        eom_ms=markers.eom_ms,
        fade_at_ms=markers.fade_at_ms,
        stop_ms=markers.stop_ms,
    )


def editor_out(view: CueEditorView) -> CueEditorOut:
    return CueEditorOut(
        file_id=view.file_id,
        title=view.title,
        artist=view.artist,
        format=view.format,
        duration_ms=view.duration_ms,
        analysis=view.analysis,
        automatic=markers_out(view.automatic),
        manual=None if view.manual is None else markers_out(view.manual),
    )


def waveform_out(waveform: Waveform) -> WaveformOut:
    return WaveformOut(
        duration_ms=waveform.duration_ms,
        per_second=waveform.per_second,
        peaks=base64.b64encode(waveform.peaks).decode("ascii"),
    )
