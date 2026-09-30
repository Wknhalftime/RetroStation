"""Schedule builders and constants for the stream service tests (spec: Testing, explicit
times; no clock calls)."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from uuid import UUID, uuid4

from backend.domain.streaming import CuePoints, PlayableFile, ScheduleItem
from backend.playout.liquidsoap_process import long_path

CALL = "KIOA"
YEAR = 1995
DAY = date(1995, 3, 14)
STATION = UUID("0000c0de-0000-4000-8000-000000000001")
NOW = datetime(2026, 3, 14, 6, 1)
"""Local real time; on DAY's station clock this is 06:01:00."""
CLOCK_OFFSET = datetime(1995, 3, 14, 6, 1) - NOW
BACKEND = "http://127.0.0.1:8010"


def at(hms: str, on: date = DAY) -> datetime:
    """A naive datetime at ``hms`` on ``on`` (a logged time, or a real time on NOW's date)."""
    return datetime.combine(on, time.fromisoformat(hms))


def song(
    hms: str,
    span_s: int | None = 200,
    *,
    on: date = DAY,
    cues: CuePoints | None = None,
    title: str = "Title",
    artist: str = "Artist",
) -> ScheduleItem:
    """One logged play; ``span_s=None`` is an unresolved play (``file=None``)."""
    file = (
        None
        if span_s is None
        else PlayableFile(
            file_id=uuid4(), path=f"D:/Music/{uuid4()}.flac", duration_ms=span_s * 1000, cues=cues
        )
    )
    return ScheduleItem(
        event_id=uuid4(), logged_at=at(hms, on), title=title, artist=artist, file=file
    )


def sent_path(item: ScheduleItem) -> str:
    """The path the service must send for ``item``."""
    assert item.file is not None
    return long_path(Path(item.file.path))
