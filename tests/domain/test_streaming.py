"""Task 1 acceptance tests: streaming value objects (spec: Domain, D9, D11)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from backend.domain.streaming import (
    Bookmark,
    CuePoints,
    InvalidStreamValueError,
    ItemRef,
    Landing,
    PlayableFile,
    ScheduleItem,
    StreamingError,
    StreamTiming,
)
from tests.domain.streaming_helpers import TIMING

GOOD_CUES = {
    "cue_in_ms": 1000,
    "cue_out_ms": 181_000,
    "fade_in_ms": 2000,
    "fade_out_ms": 5000,
    "start_next_ms": 4000,
    "gain_db": -6.0,
}


def cues(**overrides: float) -> CuePoints:
    return CuePoints(**{**GOOD_CUES, **overrides})  # type: ignore[arg-type]


def file(duration_ms: int | None, cue_points: CuePoints | None = None) -> PlayableFile:
    return PlayableFile(file_id=uuid4(), path="D:/a.flac", duration_ms=duration_ms, cues=cue_points)


def test_invalid_value_error_is_both_streaming_and_value_error() -> None:
    assert issubclass(InvalidStreamValueError, StreamingError)
    assert issubclass(InvalidStreamValueError, ValueError)


def test_cue_points_reject_cue_out_not_after_cue_in() -> None:
    with pytest.raises(InvalidStreamValueError, match="CuePoints.cue_out_ms"):
        cues(cue_in_ms=5000, cue_out_ms=5000)


@pytest.mark.parametrize("field", ["cue_in_ms", "fade_in_ms", "fade_out_ms", "start_next_ms"])
def test_cue_points_reject_negative(field: str) -> None:
    with pytest.raises(InvalidStreamValueError, match=f"CuePoints.{field}"):
        cues(**{field: -1})


def test_cue_points_start_next_must_be_inside_span() -> None:
    with pytest.raises(InvalidStreamValueError, match="CuePoints.start_next_ms"):
        cues(cue_in_ms=1000, cue_out_ms=11_000, start_next_ms=10_000)


def test_cue_points_reject_non_finite_gain() -> None:
    with pytest.raises(InvalidStreamValueError, match="CuePoints.gain_db"):
        cues(gain_db=float("nan"))


def test_stream_timing_rejects_zero_look_ahead() -> None:
    with pytest.raises(InvalidStreamValueError, match="StreamTiming.max_days_ahead"):
        StreamTiming(max_days_ahead=0)


def test_stream_timing_rejects_non_positive_window() -> None:
    with pytest.raises(InvalidStreamValueError, match="StreamTiming.window"):
        StreamTiming(window=timedelta(0))


def test_playable_file_rejects_negative_duration() -> None:
    with pytest.raises(InvalidStreamValueError, match="PlayableFile.duration_ms"):
        file(-1)


def test_span_uses_cues_when_cached() -> None:
    assert file(240_000, cues()).span_ms() == 180_000


def test_span_falls_back_to_duration_then_zero() -> None:
    assert file(240_000).span_ms() == 240_000
    assert file(None).span_ms() == 0


def test_min_span_uses_cached_fades_else_defaults() -> None:
    assert file(240_000, cues(fade_in_ms=2000, fade_out_ms=5000)).min_span_ms(TIMING) == 10_000
    assert file(240_000).min_span_ms(TIMING) == 3000 + 4000 + 3000


def test_tail_fits_needs_min_span_left() -> None:
    track = file(240_000)
    assert track.tail_fits(230_000, TIMING)
    assert not track.tail_fits(230_001, TIMING)


def test_tail_is_measured_from_cue_in() -> None:
    track = file(240_000, cues())  # span 180_000, min span 10_000
    assert track.tail_fits(170_000, TIMING)
    assert not track.tail_fits(170_001, TIMING)


def test_file_shorter_than_min_span_is_unplayable() -> None:
    assert not file(9_999).is_playable(TIMING)
    assert file(10_000).is_playable(TIMING)
    assert not file(None).is_playable(TIMING)


def test_fades_longer_than_span_are_valid_but_unplayable() -> None:
    assert not file(240_000, cues(cue_in_ms=0, cue_out_ms=8000)).is_playable(TIMING)


def test_schedule_item_without_file_is_unplayable() -> None:
    item = ScheduleItem(
        event_id=uuid4(), logged_at=datetime(1995, 3, 14, 6), title="T", artist="A", file=None
    )
    assert not item.is_playable(TIMING)


def test_schedule_item_rejects_aware_time() -> None:
    with pytest.raises(InvalidStreamValueError, match="ScheduleItem.logged_at"):
        ScheduleItem(
            event_id=uuid4(),
            logged_at=datetime(1995, 3, 14, 6, tzinfo=UTC),
            title="T",
            artist="A",
            file=None,
        )


def test_item_ref_rejects_negative_index() -> None:
    with pytest.raises(InvalidStreamValueError, match="ItemRef.index"):
        ItemRef(day=date(1995, 3, 14), index=-1)


def test_landing_rejects_negative_offset() -> None:
    with pytest.raises(InvalidStreamValueError, match="Landing.offset_ms"):
        Landing(ItemRef(date(1995, 3, 14), 0), -1)


def test_landing_advances_and_never_goes_negative() -> None:
    landing = Landing(ItemRef(date(1995, 3, 14), 2), 1000)
    assert landing.advanced_by(timedelta(seconds=9)).offset_ms == 10_000
    assert landing.advanced_by(timedelta(seconds=-5)).offset_ms == 1000


def test_landing_advance_is_exact_to_the_millisecond() -> None:
    landing = Landing(ItemRef(date(1995, 3, 14), 0), 0)
    assert landing.advanced_by(timedelta(milliseconds=1001)).offset_ms == 1001


def test_bookmark_expires_when_station_clock_passes_logged_position() -> None:
    bookmark = Bookmark(
        Landing(ItemRef(date(1995, 3, 14), 0), 180_000),
        logged_at=datetime(1995, 3, 14, 15, 0),
        left_at=datetime(2026, 3, 14, 14, 30),
        clock_offset=datetime(1995, 3, 14) - datetime(2026, 3, 14),
    )
    assert bookmark.expires_at == datetime(1995, 3, 14, 15, 3)
    assert not bookmark.is_expired(datetime(2026, 3, 14, 15, 2, 59))
    assert bookmark.is_expired(datetime(2026, 3, 14, 15, 3))


def test_bookmark_rejects_aware_times() -> None:
    with pytest.raises(InvalidStreamValueError, match="Bookmark.left_at"):
        Bookmark(
            Landing(ItemRef(date(1995, 3, 14), 0), 0),
            logged_at=datetime(1995, 3, 14, 15),
            left_at=datetime(2026, 3, 14, 14, 30, tzinfo=UTC),
            clock_offset=timedelta(0),
        )
