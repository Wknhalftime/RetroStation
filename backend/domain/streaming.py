"""Tune-in streaming: schedule positions, timing and bookmarks.

Times are naive. ``logged_at`` is station wall-clock time (what the station's log said);
``now`` and ``left_at`` are local real time. ``clock_offset`` maps real time onto the
station clock: ``station_time = real_time + clock_offset``.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from backend.domain.library import AudioHash

_ONE_MS = timedelta(milliseconds=1)


class StreamingError(Exception):
    """Base class for tune-in streaming failures."""


class InvalidStreamValueError(StreamingError, ValueError):
    """A streaming value object was built with an invalid field."""


class NoBroadcastError(StreamingError):
    """Nothing playable is scheduled for this station-year at this time."""


class EndOfScheduleError(StreamingError):
    """The schedule has no further playable item."""


class StaleScheduleError(StreamingError):
    """A position points past the end of its day: the day's log changed under the session."""


class StreamReadError(StreamingError):
    """The database could not answer a stream read within its bounds, or at all (D88)."""


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
"""Version of the cue analysis in force, recorded on each row; never checked on read (D20).

Policy (D20): an analyser change purges the old rows once. The purge exists now
(``StreamCueRepository.purge_other_versions``, PR E1): cue pre-computation runs it at the
start of every run.
"""


@dataclass(frozen=True)
class CueCandidate:
    """Audio that needs analysis (D20), and the library file it is read from.

    A read of library data, not validated: an odd ``duration_ms`` means no fallback row
    (D52, D55), and an unknown stat means the hash is trusted (D64).
    """

    file_id: UUID
    path: str
    audio_hash: AudioHash
    duration_ms: int | None
    file_size: int | None
    file_mtime_ns: int | None


@dataclass(frozen=True)
class CueAnalysis:
    """The result of analysing one audio, as stored for playback (D20).

    Keyed by the library's ``AudioHash``: every file that shares the audio shares the
    cues, and no file needs to exist for the analysis to be stored. ``analysis_failed``
    records that ``cues`` are fallback values; it is data and changes nothing on read.
    """

    audio_hash: AudioHash
    cues: CuePoints
    loudness_lufs: float | None
    analysis_failed: bool
    analyser_version: int

    def __post_init__(self) -> None:
        if self.analyser_version < 1:
            raise InvalidStreamValueError(
                f"CueAnalysis.analyser_version must be >= 1, got {self.analyser_version}"
            )
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


@dataclass(frozen=True)
class StationYear:
    """A station-year a listener can tune in to, and the days its log covers (D70).

    Coverage is days logged only ("362 of 365 days"): no per-day file check and no
    whole-year percentage.
    """

    call_letters: str
    year: int
    days_logged: int

    def __post_init__(self) -> None:
        if not self.call_letters:
            raise InvalidStreamValueError("StationYear.call_letters must not be empty")
        if not 1 <= self.year <= 9999:
            raise InvalidStreamValueError(f"StationYear.year must be 1..9999, got {self.year}")
        if not 1 <= self.days_logged <= self.days_in_year:
            raise InvalidStreamValueError(
                f"StationYear.days_logged must be 1..{self.days_in_year}, got {self.days_logged}"
            )

    @property
    def days_in_year(self) -> int:
        """366 in a leap year, else 365."""
        return 366 if calendar.isleap(self.year) else 365
