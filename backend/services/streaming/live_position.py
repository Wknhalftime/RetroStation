"""An open session's live position: where its listener is now (D109).

A same-key reconnect lands there instead of tuning in by the clock: the channel's newest
open session that has started playing, at its committed item plus the time since that item
started on the steady clock (D47). A live position beats any bookmark on the channel, and no
expiry applies to it (coordinator rulings).

The position is read on the event loop, which owns session state, so it cannot race the
session's own reports (``newest_live_position``). The placement thread then checks it
against the day as logged now and walks it forward, as a bookmark resume does (D11:
``live_landing``). The session it was read from is never changed (D78c).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from backend.domain.streaming import DayLoader, Landing, StreamTiming
from backend.domain.tune_in import played_on
from backend.services.streaming.bookmarks import BookmarkKey, still_logged_at
from backend.services.streaming.sessions import Assigned, StreamSession, schedule_finished

__all__ = ["LivePosition", "live_landing", "live_position", "newest_live_position"]


@dataclass(frozen=True)
class LivePosition:
    """An open session's position, read at one moment on the event loop."""

    session_id: str
    """The open session it was read from."""
    started: Landing
    """The committed item, at the offset it started playing from."""
    event_id: UUID
    """The play the session found at ``started.ref`` when it read its day."""
    heard: timedelta
    """Time since the committed item started, on the steady clock (D47)."""
    clock_offset: timedelta
    """The session's station-clock offset, which a stream placed here carries on with."""


def live_position(
    session_id: str, session: StreamSession, elapsed_now: datetime
) -> LivePosition | None:
    """``session``'s position at ``elapsed_now`` (an elapsed-clock reading), or ``None`` when
    it has none to resume from: nothing has started yet, or its final item has started (it
    signed off, D26). Unlike the service's ``_left_at`` (a bookmark), the start offset and
    the time since are kept apart, so ``played_on`` can drop a stale offset (D11)."""
    committed = session.committed
    if committed is None or session.clock_offset is None or schedule_finished(session):
        return None
    playing = committed.assigned
    if not isinstance(playing, Assigned):  # the sign-off clip has no place in the log
        return None
    return LivePosition(
        session_id=session_id,
        started=Landing(playing.ref, playing.offset_ms),
        event_id=playing.item.event_id,
        heard=elapsed_now - committed.started_at,
        clock_offset=session.clock_offset,
    )


def newest_live_position(
    sessions: dict[str, StreamSession], channel: BookmarkKey, elapsed_now: datetime
) -> LivePosition | None:
    """The live position of ``channel``'s newest open session that has one: the latest
    admitted, the stream the page follows (D78c). ``sessions`` is in admission order; a
    session still being placed has nothing committed, so it is passed over."""
    for session_id, session in reversed(sessions.items()):
        if session.bookmark_key != channel:
            continue
        position = live_position(session_id, session, elapsed_now)
        if position is not None:
            return position
    return None


def live_landing(load_day: DayLoader, live: LivePosition, timing: StreamTiming) -> Landing | None:
    """Where ``live`` has got to in the day as ``load_day`` reads it now, or ``None`` when it
    is not to be used: its play is no longer logged there (D11's check), or the schedule
    ends before it (the station has signed off since). Past the end of its item, it walks on
    to the item the station is on."""
    if not still_logged_at(load_day, live.started.ref, live.event_id):
        return None
    return played_on(load_day, live.started, live.heard, timing)
