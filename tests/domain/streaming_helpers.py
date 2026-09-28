"""Schedule builders for tune-in tests: explicit times, no clock."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from uuid import uuid4

from backend.domain.streaming import PlayableFile, ScheduleItem, StreamTiming

TIMING = StreamTiming()
D = date(1995, 3, 14)
NEXT = date(1995, 3, 15)
PREV = date(1995, 3, 13)
OFFSET_1995 = datetime(1995, 3, 14) - datetime(2026, 3, 14)

type Play = tuple[str, int | None]
"""(logged "HH:MM:SS", playable span in seconds, or None for an unmatched play)."""


def now_at(hms: str, on: date = date(2026, 3, 14)) -> datetime:
    """Local real time on ``on`` (default 14 March 2026, which maps onto ``D``)."""
    return datetime.combine(on, time.fromisoformat(hms))


def song(span_s: int | None) -> PlayableFile | None:
    if span_s is None:
        return None
    return PlayableFile(
        file_id=uuid4(), path=f"D:/music/{uuid4()}.flac", duration_ms=span_s * 1000, cues=None
    )


def day(on: date, plays: Sequence[Play]) -> list[ScheduleItem]:
    return [
        ScheduleItem(
            event_id=uuid4(),
            logged_at=datetime.combine(on, time.fromisoformat(at)),
            title=f"Song at {at}",
            artist="Artist",
            file=song(span_s),
        )
        for at, span_s in plays
    ]


class DictDayLoader:
    """A DayLoader over a dict: missing days are empty."""

    def __init__(self, days: dict[date, list[ScheduleItem]]) -> None:
        self._days = days

    def __call__(self, on: date) -> Sequence[ScheduleItem]:
        return self._days.get(on, [])
