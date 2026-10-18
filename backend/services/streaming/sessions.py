"""Per-session state: a memoised day loader, and the mutable ``StreamSession`` it belongs to.

Every later read of a session's day is served from this memo (carried item C9), so the day
is read at most once per day per session; the load happens under a lock because session
state is otherwise only ever touched on the event loop (this is the one exception, per the
global constraints: "Only the day memo is touched off the event loop").
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from backend.domain.liners import LinerCadence, LinerRotation, PlayableLiner
from backend.domain.streaming import DayLoader, ItemRef, Landing, ScheduleItem
from backend.playout.liquidsoap_process import RunningEngine
from backend.services.streaming.bookmarks import BookmarkKey
from backend.services.streaming.payload import FinalClip

__all__ = [
    "Assigned",
    "Committed",
    "LinerSlot",
    "StreamSession",
    "memoised_day_loader",
    "schedule_finished",
    "song_heard",
    "songs_since_liner",
]


def memoised_day_loader(load: DayLoader) -> DayLoader:
    """Wrap ``load`` so each day is read at most once, even under concurrent first reads."""
    cache: dict[date, Sequence[ScheduleItem]] = {}
    lock = threading.Lock()

    def load_once(day: date) -> Sequence[ScheduleItem]:
        with lock:
            if day not in cache:
                cache[day] = load(day)
            return cache[day]

    return load_once


@dataclass(frozen=True)
class Assigned:
    """What seq was assigned to: the position, its item, and the offset it lands at."""

    ref: ItemRef
    item: ScheduleItem
    offset_ms: int


@dataclass(frozen=True)
class LinerSlot:
    """A liner handed out for a seq: which liner, where its file is stored, and the song it
    follows. ``after`` is that song's assignment and ``after_span_ms`` the span it was sent
    with, so the liner can be started at the song's EOM (LR6). ``next_song`` is the song the
    stream walked to after ``after``, at its top: where a reconnect or a bookmark during the
    liner is placed (LR9). It is ``None`` only in slots built by hand for their payload."""

    liner: PlayableLiner
    path: Path
    after: Assigned
    after_span_ms: int
    next_song: Assigned | None = None

    @property
    def span_ms(self) -> int:
        """How long the liner plays, from its cue-in to its cue-out."""
        cues = self.liner.cues
        return cues.cue_out_ms - cues.cue_in_ms


@dataclass(frozen=True)
class Committed:
    """The position a ``started`` report has confirmed, with when it started.

    ``assigned`` is a regular item, a station liner, or the sign-off clip (D26) once the
    schedule has ended with one configured — a liner or clip has no ``ItemRef``/
    ``ScheduleItem`` to offer.
    ``started_at`` is an elapsed-clock reading (D47), comparable only with other readings of
    that clock, never with the wall clock or the domain's station times.
    """

    seq: int
    assigned: Assigned | FinalClip | LinerSlot
    started_at: datetime


@dataclass
class StreamSession:
    """One listener's mutable state: no behaviour, just the fields ``StreamService`` reads
    and writes.

    ``load_day`` is expected to already be memoised (``memoised_day_loader``). ``token`` is
    compared, with ``secrets.compare_digest``, against every ``item``/``started``/``failed``
    call. ``bookmark_key`` is ``None`` for a keyless (D28) tune-in. ``landing`` and
    ``clock_offset`` come from placement, which runs after admission (R1), so they start
    ``None`` and are set as soon as placement returns, while the engine may still be
    starting; ``placed`` is set once placement has ended either way, so the engine's first
    ``item`` request (seq 0, often before its start is confirmed) can wait for the landing
    instead of being told to retry. ``assigned`` maps seq to what was handed
    out for it — an item, or the final clip — so "the same seq always returns the same
    item"; ``end_seq`` is the end-of-schedule marker. ``final_failed`` records that the
    engine reported the final clip failed (PG5): it is never committed, since the engine may
    fetch it, and fail it, before the last song starts. ``stopped`` marks a session the
    watchdog (or ``close``) has already stopped, so it is not frozen twice. ``now_playing``
    is ``(shows_at, text)``: the query compares ``shows_at`` to the clock rather than the
    record flipping itself, per CQRS.

    ``station_id``, ``liner_cadence`` and ``liner_rotation`` are the station's liners on
    this stream (LR7): which station, how often (``None`` is off), and the shuffle state.

    ``opened_at`` and ``now_playing[0]`` (``shows_at``) are elapsed-clock readings (D47), like
    ``Committed.started_at``: comparable only with each other, never with the wall clock or
    the domain's station times.
    """

    load_day: DayLoader
    opened_at: datetime
    bookmark_key: BookmarkKey | None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    landing: Landing | None = None
    clock_offset: timedelta | None = None
    placed: asyncio.Event = field(default_factory=asyncio.Event)
    assigned: dict[int, Assigned | FinalClip | LinerSlot] = field(default_factory=dict)
    committed: Committed | None = None
    end_seq: int | None = None
    final_failed: bool = False
    engine: RunningEngine | None = None
    now_playing: tuple[datetime, str] | None = None
    stopped: bool = False
    station_id: UUID | None = None
    liner_cadence: LinerCadence | None = None
    liner_rotation: LinerRotation = LinerRotation()


def schedule_finished(session: StreamSession) -> bool:
    """The schedule has ended and its last item that can play has started (D26): the last
    assigned one, or, when the final clip failed (PG5), the song before it."""
    committed = session.committed
    if session.end_seq is None or committed is None:
        return False
    last_to_play = session.end_seq - (2 if session.final_failed else 1)
    return committed.seq >= last_to_play  # a clip reported started, then failed


def song_heard(committed: Committed, elapsed_now: datetime) -> tuple[Assigned, timedelta] | None:
    """The committed song and how much of it has played at ``elapsed_now`` (an elapsed-clock
    reading), or ``None`` for the sign-off clip, which has no place in the log.

    Liners are timeless (LR9, plan note 12): while one plays, the listener is at the top of
    the song the stream walked to after the song before it, with nothing of it heard, so no
    liner time is counted. That song is the walk's, not the song before plus its sent span
    measured against the day as read at tune-in, which a D80 landing or a D85 re-read would
    miss (#171 review M3). A slot without one is placed the old way: the song before, played
    for its sent span."""
    playing = committed.assigned
    if isinstance(playing, LinerSlot):
        if playing.next_song is not None:
            return playing.next_song, timedelta(0)
        return playing.after, timedelta(milliseconds=playing.after_span_ms)
    if isinstance(playing, Assigned):
        return playing, elapsed_now - committed.started_at
    return None


def songs_since_liner(session: StreamSession, before_seq: int) -> int:
    """The songs handed out before ``before_seq`` since the session's last liner, or since
    tune-in when it has had none (LR7): the landing is song 1, and a liner restarts the count."""
    count = 0
    for seq in range(before_seq):
        handed_out = session.assigned.get(seq)
        if isinstance(handed_out, LinerSlot):
            count = 0
        elif isinstance(handed_out, Assigned):
            count += 1
    return count
