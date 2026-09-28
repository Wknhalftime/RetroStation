"""Tune-in streaming: schedule positions, timing and bookmarks.

Times are naive. ``logged_at`` is station wall-clock time (what the station's log said);
``now`` and ``left_at`` are local real time. ``clock_offset`` maps real time onto the
station clock: ``station_time = real_time + clock_offset``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

_ONE_MS = timedelta(milliseconds=1)


class StreamingError(Exception):
    """Base class for tune-in streaming failures."""


class InvalidStreamValueError(StreamingError, ValueError):
    """A streaming value object was built with an invalid field."""


class NoBroadcastError(StreamingError):
    """Nothing playable is scheduled for this station-year at this time."""


class EndOfScheduleError(StreamingError):
    """The schedule has no further playable item."""


class CueFileNotFoundError(StreamingError):
    """Cues were stored for a library file that no longer exists."""

    def __init__(self, file_id: UUID) -> None:
        super().__init__(f"no library file with file_id={file_id} to store cues for")
        self.file_id = file_id


class StaleScheduleError(StreamingError):
    """A position points past the end of its day: the day's log changed under the session."""


def to_ms(delta: timedelta) -> int:
    """Whole milliseconds in ``delta``, exactly (no float rounding)."""
    return delta // _ONE_MS


def _require_non_negative(owner: str, **fields: int) -> None:
    for name, value in fields.items():
        if value < 0:
            raise InvalidStreamValueError(f"{owner}.{name} must be >= 0, got {value}")


def _require_naive(owner: str, **fields: datetime) -> None:
    for name, value in fields.items():
        if value.tzinfo is not None:
            raise InvalidStreamValueError(
                f"{owner}.{name} must be a naive datetime, got {value.isoformat()}"
            )


@dataclass(frozen=True)
class CuePoints:
    """Analysed playback points of one file, in ms from the start of the file.

    ``start_next_ms`` counts back from ``cue_out_ms``: the next deck starts that long before
    this one ends.
    """

    cue_in_ms: int
    cue_out_ms: int
    fade_in_ms: int
    fade_out_ms: int
    start_next_ms: int
    gain_db: float

    def __post_init__(self) -> None:
        _require_non_negative(
            "CuePoints",
            cue_in_ms=self.cue_in_ms,
            fade_in_ms=self.fade_in_ms,
            fade_out_ms=self.fade_out_ms,
            start_next_ms=self.start_next_ms,
        )
        if self.cue_out_ms <= self.cue_in_ms:
            raise InvalidStreamValueError(
                f"CuePoints.cue_out_ms ({self.cue_out_ms}) must be greater than "
                f"cue_in_ms ({self.cue_in_ms})"
            )
        if self.start_next_ms >= self.cue_out_ms - self.cue_in_ms:
            raise InvalidStreamValueError(
                f"CuePoints.start_next_ms ({self.start_next_ms}) must be less than the span "
                f"({self.cue_out_ms - self.cue_in_ms})"
            )
        if not math.isfinite(self.gain_db):
            raise InvalidStreamValueError(f"CuePoints.gain_db must be finite, got {self.gain_db}")


CUE_ANALYSER_VERSION = 1
"""Version of the cue analysis in force. Stored rows of any other version are stale."""


@dataclass(frozen=True)
class CueAnalysis:
    """The result of analysing one library file, as stored for playback.

    ``file_size`` and ``file_mtime_ns`` are the file's stat when it was analysed; a
    later mismatch with the library makes the stored cues stale. ``analysis_failed``
    records that ``cues`` are fallback values; it is data and changes nothing on read.
    """

    file_id: UUID
    cues: CuePoints
    loudness_lufs: float | None
    analysis_failed: bool
    analyser_version: int
    file_size: int | None
    file_mtime_ns: int | None

    def __post_init__(self) -> None:
        if self.analyser_version < 1:
            raise InvalidStreamValueError(
                f"CueAnalysis.analyser_version must be >= 1, got {self.analyser_version}"
            )
        if self.file_size is not None:
            _require_non_negative("CueAnalysis", file_size=self.file_size)
        if self.loudness_lufs is not None and not math.isfinite(self.loudness_lufs):
            raise InvalidStreamValueError(
                f"CueAnalysis.loudness_lufs must be finite, got {self.loudness_lufs}"
            )


@dataclass(frozen=True)
class StreamTiming:
    """Timing rules shared by tune-in, the walk and next-item selection."""

    default_fade_in_ms: int = 3000
    default_fade_out_ms: int = 4000
    margin_ms: int = 3000
    window: timedelta = timedelta(hours=3)
    max_days_ahead: int = 2

    def __post_init__(self) -> None:
        _require_non_negative(
            "StreamTiming",
            default_fade_in_ms=self.default_fade_in_ms,
            default_fade_out_ms=self.default_fade_out_ms,
            margin_ms=self.margin_ms,
        )
        if self.window <= timedelta(0):
            raise InvalidStreamValueError(f"StreamTiming.window must be > 0, got {self.window}")
        if self.max_days_ahead < 1:
            raise InvalidStreamValueError(
                f"StreamTiming.max_days_ahead must be >= 1, got {self.max_days_ahead}"
            )


@dataclass(frozen=True)
class PlayableFile:
    """The library file a play resolves to."""

    file_id: UUID
    path: str
    duration_ms: int | None
    cues: CuePoints | None

    def __post_init__(self) -> None:
        if self.duration_ms is not None:
            _require_non_negative("PlayableFile", duration_ms=self.duration_ms)

    def span_ms(self) -> int:
        """Audible length: cue-in to cue-out when analysed, else the tagged duration."""
        if self.cues is not None:
            return self.cues.cue_out_ms - self.cues.cue_in_ms
        return self.duration_ms or 0

    def min_span_ms(self, timing: StreamTiming) -> int:
        """Shortest audible stretch that fades in and out cleanly (spec D9)."""
        fade_in = self.cues.fade_in_ms if self.cues else timing.default_fade_in_ms
        fade_out = self.cues.fade_out_ms if self.cues else timing.default_fade_out_ms
        return fade_in + fade_out + timing.margin_ms

    def tail_fits(self, offset_ms: int, timing: StreamTiming) -> bool:
        """Whether starting ``offset_ms`` after cue-in leaves the minimum span (D9)."""
        return self.span_ms() - offset_ms >= self.min_span_ms(timing)

    def is_playable(self, timing: StreamTiming) -> bool:
        return self.span_ms() > 0 and self.tail_fits(0, timing)


@dataclass(frozen=True)
class ScheduleItem:
    """One logged play, in logged order, with the file it resolves to (or none).

    A day's items must include every logged play (unresolved ones with ``file=None``), so
    that an ``ItemRef`` index stays stable for the life of a session.
    """

    event_id: UUID
    logged_at: datetime
    title: str
    artist: str
    file: PlayableFile | None

    def __post_init__(self) -> None:
        _require_naive("ScheduleItem", logged_at=self.logged_at)

    def is_playable(self, timing: StreamTiming) -> bool:
        return self.file is not None and self.file.is_playable(timing)


type DayLoader = Callable[[date], Sequence[ScheduleItem]]
"""Returns one station day in logged order; an empty sequence when the day has no log.

Within one operation the same day may be requested more than once, so a loader must
return identical results for a given day for the life of a session (PR D memoises it
per session).
"""


@dataclass(frozen=True)
class ItemRef:
    """Position of a play: its day and its index in that day's logged order."""

    day: date
    index: int

    def __post_init__(self) -> None:
        _require_non_negative("ItemRef", index=self.index)


@dataclass(frozen=True)
class Landing:
    """Where playback starts: an item, and ms after its cue-in.

    The engine plays from ``cue_in_ms + offset_ms`` (``liq_cue_in``).
    """

    ref: ItemRef
    offset_ms: int

    def __post_init__(self) -> None:
        _require_non_negative("Landing", offset_ms=self.offset_ms)

    def advanced_by(self, elapsed: timedelta) -> Landing:
        """The same item, further in by ``elapsed``; negative elapsed counts as zero."""
        return Landing(self.ref, self.offset_ms + to_ms(max(timedelta(0), elapsed)))


@dataclass(frozen=True)
class TuneIn:
    """Result of a clock tune-in."""

    landing: Landing
    clock_offset: timedelta


@dataclass(frozen=True)
class Bookmark:
    """Where a listener left: ``landing`` in an item logged at ``logged_at`` (spec D11)."""

    landing: Landing
    logged_at: datetime
    left_at: datetime
    clock_offset: timedelta

    def __post_init__(self) -> None:
        _require_naive("Bookmark", logged_at=self.logged_at, left_at=self.left_at)

    @property
    def expires_at(self) -> datetime:
        """The station time at which the bookmarked position stops being ahead."""
        return self.logged_at + timedelta(milliseconds=self.landing.offset_ms)

    def is_expired(self, now: datetime) -> bool:
        """True once the station clock has passed the bookmarked position."""
        _require_naive("Bookmark.is_expired", now=now)
        return now + self.clock_offset >= self.expires_at
