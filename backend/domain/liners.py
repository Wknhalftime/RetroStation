"""Station liners: a station's own audio clips, played between songs at its cadence.

Errors sit at the top, like ``domain/streaming.py``. A liner's cues are its own: autocue's
gain and EOM, never a fade-in or an analysed fade-out; a manual edit's fade is always its
Stop (plan notes 6 and 7).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from backend.domain.library import AudioHash
from backend.domain.streaming import (
    NO_CUES_GAIN_DB,
    ClipFormat,
    CueAnalysis,
    CueMarkers,
    CuePoints,
)


class LinerError(Exception):
    """Base class for station-liner failures."""


class InvalidLinerValueError(LinerError, ValueError):
    """A liner value object was built with an invalid field."""


class LinerNotFoundError(LinerError):
    """The station has no liner with this id."""


class DuplicateLinerError(LinerError):
    """The station already has a liner with this audio."""


class LinerTooLargeError(LinerError):
    """The liner is larger than ``MAX_LINER_BYTES``."""


class LinerStorageError(LinerError):
    """The liner or its row could not be stored: a disk or database failure."""


MAX_LINER_BYTES = 200 * 2**20
MAX_LINER_NAME = 255
MIN_CADENCE = 1
MAX_CADENCE = 1000

LINER_FILE_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(flac|mp3|wav)$"
)
"""A stored liner's file name: its id, then its format."""


@dataclass(frozen=True)
class LinerCadence:
    """A liner plays after every ``songs`` songs; a station with no cadence has liners off."""

    songs: int

    def __post_init__(self) -> None:
        if not MIN_CADENCE <= self.songs <= MAX_CADENCE:
            raise InvalidLinerValueError(
                f"LinerCadence.songs must be {MIN_CADENCE}-{MAX_CADENCE}, got {self.songs}"
            )

    @classmethod
    def from_setting(cls, value: int | None) -> LinerCadence | None:
        """The cadence stored as *value*; a blank setting (None) means liners are off."""
        return None if value is None else cls(value)


@dataclass(frozen=True)
class StationLiner:
    """One liner of one station: its display name, stored file, audio and optional edit."""

    id: UUID
    station_id: UUID
    name: str
    file_name: str
    format: ClipFormat
    audio_hash: AudioHash
    duration_ms: int
    markers: CueMarkers | None
    uploaded_at: datetime

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not 1 <= len(name) <= MAX_LINER_NAME:
            raise InvalidLinerValueError(
                f"StationLiner.name must be 1-{MAX_LINER_NAME} characters, got {len(name)}"
            )
        object.__setattr__(self, "name", name)
        expected = f"{self.id}.{self.format.value}"
        if self.file_name != expected or LINER_FILE_NAME.fullmatch(self.file_name) is None:
            raise InvalidLinerValueError(
                f"StationLiner.file_name must be {expected!r}, got {self.file_name!r}"
            )
        if self.duration_ms <= 0:
            raise InvalidLinerValueError(
                f"StationLiner.duration_ms must be > 0, got {self.duration_ms}"
            )
        if self.uploaded_at.tzinfo is None:
            raise InvalidLinerValueError("StationLiner.uploaded_at must be timezone-aware")
        markers = self.markers
        if markers is not None:
            if markers.fade_at_ms != markers.stop_ms:
                raise InvalidLinerValueError(
                    f"StationLiner.markers fade ({markers.fade_at_ms}) must sit at "
                    f"stop ({markers.stop_ms})"
                )
            if markers.stop_ms > self.duration_ms:
                raise InvalidLinerValueError(
                    f"StationLiner.markers stop ({markers.stop_ms}) is past the "
                    f"duration ({self.duration_ms})"
                )


class LinerCueState(StrEnum):
    """Where a liner's cues came from, as the liners tab lists it."""

    EDITED = "edited"
    AUTO = "auto"
    FAILED = "failed"
    ANALYSING = "analysing"
    SILENT = "silent"


@dataclass(frozen=True)
class LinerEntry:
    """A liner as the tab lists it, with the state of its cues."""

    liner: StationLiner
    cue_state: LinerCueState


@dataclass(frozen=True)
class PlayableLiner:
    """A liner the radio can play: its stored file and the cue points to play it with."""

    liner_id: UUID
    file_name: str
    cues: CuePoints


def liner_cue_state(markers: CueMarkers | None, stored: CueAnalysis | None) -> LinerCueState:
    """Silent wins, then edited, then failed, then auto; analysing when nothing is stored."""
    if stored is not None and stored.audio_silent:
        return LinerCueState.SILENT
    if markers is not None:
        return LinerCueState.EDITED
    if stored is None:
        return LinerCueState.ANALYSING
    return LinerCueState.FAILED if stored.analysis_failed else LinerCueState.AUTO


def liner_markers(start_ms: int, eom_ms: int, stop_ms: int) -> CueMarkers:
    """A liner's edit: its fade-out is always its Stop (plan note 7)."""
    return CueMarkers(start_ms, eom_ms, stop_ms, stop_ms)


def liner_points(
    duration_ms: int, markers: CueMarkers | None, stored: CueAnalysis | None
) -> CuePoints | None:
    """The points a liner plays with, or None when its audio is silent (plan note 6).

    An edit gives the positions and the stored row the gain; with no edit the stored row
    gives both. Either way there is no fade-in and no fade-out. With neither, the whole
    file plays at the no-cues gain.
    """
    if stored is not None and stored.audio_silent:
        return None
    if markers is not None:
        gain_db = NO_CUES_GAIN_DB if stored is None else stored.cues.gain_db
        return markers.to_points(gain_db)
    if stored is None:
        return CuePoints(0, duration_ms, 0, 0, 0, NO_CUES_GAIN_DB)
    return replace(stored.cues, fade_in_ms=0, fade_out_ms=0)


type Shuffle = Callable[[list[UUID]], None]
"""Shuffles the list in place; ``random.shuffle`` in production."""


@dataclass(frozen=True)
class LinerRotation:
    """A session's liner shuffle: the ids still to play, and the liner played last."""

    order: tuple[UUID, ...] = ()
    last: UUID | None = None


def next_liner(
    rotation: LinerRotation, available: Sequence[UUID], shuffle: Shuffle
) -> tuple[UUID | None, LinerRotation]:
    """The next liner to play and the rotation after it (plan note 11).

    Ids no longer available are dropped from the order. With none left it reshuffles the
    available ids; if that puts the last played first (and there is more than one), the
    first and last entries swap. A liner added mid-order joins at the next reshuffle.
    """
    present = set(available)
    order = [liner_id for liner_id in rotation.order if liner_id in present]
    if not order and available:
        order = list(available)
        shuffle(order)
        if len(order) > 1 and order[0] == rotation.last:
            order[0], order[-1] = order[-1], order[0]
    if not order:
        return None, LinerRotation((), rotation.last)
    picked, *rest = order
    return picked, LinerRotation(tuple(rest), picked)


def liner_due(songs_since_liner: int, cadence: LinerCadence | None) -> bool:
    """A liner is due once ``cadence`` songs have played since the last; never without one."""
    return cadence is not None and songs_since_liner >= cadence.songs
