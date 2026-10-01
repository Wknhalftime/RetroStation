"""The listener feed: who is told what, per channel (D13: now-playing goes backend -> browser;
D73: a title carries artist and title only; D74: the channel is the bookmark key and the newest
stream owns it; D78a: the stream's status kinds; D78b: the subscription cap, 16 pending events
per subscription, and a refusal never replaces the owner's pending title; D78c: when the newest
stream ends while an older one is still playing, the next newest still-playing stream takes the
channel back — nothing is told at the hand-back, then its own next title and its close are told
again).

A title is told ``now_playing_delay`` after its item starts (the contract's "~1 s delay"). The
wait is the injected ``sleep`` and the time is read on the injected elapsed clock, so the delay
is measured on the steady clock, never on the wall clock (D47).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import partial

from backend.domain.streaming import InvalidStreamValueError
from backend.services.streaming.bookmarks import BookmarkKey
from backend.services.streaming.errors import SubscriptionLimitError

__all__ = [
    "PENDING_LIMIT",
    "ListenerEvent",
    "ListenerEvents",
    "ListenerFeed",
    "NowPlaying",
    "Sleep",
    "Status",
    "StatusKind",
]

PENDING_LIMIT = 16
"""Events one subscription may hold unread; one more and it is disconnected (D78b)."""


class StatusKind(StrEnum):
    """The stream's statuses, as the page reads them (D78a)."""

    TUNING = "tuning"
    NO_BROADCAST = "no_broadcast"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    ENDED = "ended"
    """Signed off: the page does not reconnect."""
    STOPPED = "stopped"
    """Dropped: the page reconnects and resumes."""


@dataclass(frozen=True)
class NowPlaying:
    """The song a channel is playing: artist and title, and nothing else (D73)."""

    artist: str
    title: str


@dataclass(frozen=True)
class Status:
    """A change in the stream's state (D78a)."""

    kind: StatusKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StatusKind):
            raise InvalidStreamValueError(f"Status.kind must be a StatusKind, got {self.kind!r}")


type ListenerEvent = NowPlaying | Status
type Sleep = Callable[[float], Awaitable[None]]

_TUNING = Status(StatusKind.TUNING)


@dataclass(frozen=True)
class _Held:
    """A title waiting to be told: it shows at ``shows_at`` on the elapsed clock."""

    title: NowPlaying
    shows_at: datetime


class _Mailbox:
    """One subscription's unread events: statuses in the order told, plus at most one title
    waiting for its delay. Event-loop only."""

    def __init__(self, elapsed: Callable[[], datetime], sleep: Sleep, held: _Held | None) -> None:
        self._elapsed = elapsed
        self._sleep = sleep
        self._queue: deque[ListenerEvent] = deque()
        self._held = held
        self._shut = False
        self._waker: asyncio.Future[None] | None = None

    @property
    def backlog(self) -> int:
        """How many events are held unread, the waiting title included."""
        return len(self._queue) + (self._held is not None)

    @property
    def is_shut(self) -> bool:
        return self._shut

    def tell(self, status: Status) -> None:
        """A refusal: told at once, and the waiting title stays (D78b, review I5)."""
        self._queue.append(status)
        self._wake()

    def supersede(self, status: Status) -> None:
        """A status that ends the current title: the waiting title goes (or is told first, if
        its delay has already passed), then the status is told."""
        self._replace_title(None)
        self.tell(status)

    def hold(self, held: _Held) -> None:
        """A newer title: it replaces the waiting one, which is told only if already due."""
        self._replace_title(held)
        self._wake()

    def shut(self) -> None:
        """The subscription is over: anything unread is dropped and the reader ends."""
        self._shut = True
        self._queue.clear()
        self._held = None
        self._wake()

    def take(self) -> ListenerEvent | None:
        """The next event that may be told now, removed from the mailbox; ``None`` if none."""
        if self._queue:
            return self._queue.popleft()
        held = self._held
        if held is not None and held.shows_at <= self._elapsed():
            self._held = None
            return held.title
        return None

    async def wait(self) -> None:
        """Until something is told, the mailbox shuts, or the waiting title's delay passes.

        Takes nothing, so a cancelled wait loses nothing; the sleep it started is cancelled
        and finished before it returns or raises."""
        waker: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waker = waker
        held = self._held
        delay = None if held is None else asyncio.ensure_future(self._sleep(self._remaining(held)))
        waits = {waker} if delay is None else {waker, delay}
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._waker = None
            await _cancel_all(waits)
        if _slept(delay) and held is not None and self._held is held:
            self._held = None
            self._queue.append(held.title)

    def _remaining(self, held: _Held) -> float:
        return (held.shows_at - self._elapsed()).total_seconds()

    def _replace_title(self, held: _Held | None) -> None:
        old = self._held
        if old is not None and old.shows_at <= self._elapsed():
            self._queue.append(old.title)
        self._held = held

    def _wake(self) -> None:
        if self._waker is not None and not self._waker.done():
            self._waker.set_result(None)


async def _cancel_all(futures: set[asyncio.Future[None]]) -> None:
    """Cancel ``futures`` and wait until each is finished (a cancelled sleep is gone)."""
    for future in futures:
        future.cancel()
    await asyncio.wait(futures)


def _slept(delay: asyncio.Future[None] | None) -> bool:
    """Whether ``delay`` ran its full wait; a sleep that failed raises its error here."""
    if delay is None or not delay.done() or delay.cancelled():
        return False
    delay.result()
    return True


class ListenerEvents(AsyncIterator[ListenerEvent]):
    """One subscription to a channel: the events told on it, in order.

    Reading is cancellation-safe: an event is taken only as it is returned, so a read that is
    cancelled loses nothing. The iterator ends once the subscription is closed or disconnected
    (more than ``PENDING_LIMIT`` events held unread, D78b). One reader per subscription: a
    second concurrent reader is unsupported."""

    def __init__(self, mailbox: _Mailbox, leave: Callable[[], None]) -> None:
        self._mailbox = mailbox
        self._leave = leave

    async def __anext__(self) -> ListenerEvent:
        while not self._mailbox.is_shut:
            told = self._mailbox.take()
            if told is not None:
                return told
            await self._mailbox.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        """Unsubscribe: the iterator ends, and the subscription no longer counts (D78b)."""
        self._leave()


@dataclass
class _Channel:
    """One channel's state: its still-playing streams, its title, and its subscriptions.

    ``still_playing`` holds every session admitted to this channel that has not yet been told
    ``ended``, oldest first. The newest owns the channel (D74); when it ends, the next newest
    still-playing stream takes the channel back at once (D78c)."""

    still_playing: list[str] = field(default_factory=list)
    current: _Held | None = None
    mailboxes: list[_Mailbox] = field(default_factory=list)

    @property
    def owner(self) -> str | None:
        """The newest still-playing stream, or ``None`` if none is playing (D74, D78c)."""
        return self.still_playing[-1] if self.still_playing else None

    @property
    def idle(self) -> bool:
        return not self.still_playing and not self.mailboxes


class ListenerFeed:
    """Who is told what, per channel: the newest stream's events, fanned out to the channel's
    subscriptions. When the newest stream ends while an older one is still playing, the next
    newest still-playing stream takes the channel back — its own next title, and its close,
    are told again (D78c). Event-loop only: every method must be called on the loop (the
    internal routes are ``async def``; ``close`` runs from the relay, the lifespan and the
    watchdog task)."""

    def __init__(self, elapsed: Callable[[], datetime], sleep: Sleep) -> None:
        self._elapsed = elapsed
        self._sleep = sleep
        self._channels: dict[BookmarkKey, _Channel] = {}
        self._open = 0

    @property
    def channels(self) -> int:
        """Channels with an owner or a subscription."""
        return len(self._channels)

    def opened(self, channel: BookmarkKey, session_id: str) -> None:
        """The session becomes the channel's stream (the newest wins, D74): its title is
        cleared, and ``tuning`` is told."""
        state = self._channels.setdefault(channel, _Channel())
        state.still_playing.append(session_id)
        state.current = None
        self._tell(channel, lambda mailbox: mailbox.supersede(_TUNING))

    def refused(self, channel: BookmarkKey, kind: StatusKind) -> None:
        """A tune-in was refused: ``kind`` is told; the owner and its title stay (D78b)."""
        status = Status(kind)
        self._tell(channel, lambda mailbox: mailbox.tell(status))

    def title(
        self, channel: BookmarkKey, session_id: str, title: NowPlaying, shows_at: datetime
    ) -> None:
        """If the session owns the channel, ``title`` becomes its title, told at ``shows_at``."""
        state = self._channels.get(channel)
        if state is None or state.owner != session_id:
            return
        held = _Held(title, shows_at)
        state.current = held
        self._tell(channel, lambda mailbox: mailbox.hold(held))

    def ended(self, channel: BookmarkKey, session_id: str, kind: StatusKind) -> None:
        """The session has ended: it stops being tracked as still playing. A session the feed
        has already been told ``ended`` for is ignored and never takes the channel back.

        If the session owned the channel, ``kind`` is told and the title clears; the next
        newest still-playing stream, if any, takes the channel back at once. Nothing is told
        for that hand-back — its own next title, and its close, are told when they happen
        (D78c)."""
        state = self._channels.get(channel)
        if state is None or session_id not in state.still_playing:
            return
        was_owner = state.owner == session_id
        state.still_playing.remove(session_id)
        if was_owner:
            state.current = None
            status = Status(kind)
            self._tell(channel, lambda mailbox: mailbox.supersede(status))
        self._prune(channel, state)

    def subscribe(self, channel: BookmarkKey, limit: int) -> ListenerEvents:
        """A new subscription, holding the channel's current title first; refused with
        ``SubscriptionLimitError`` when ``limit`` are already open app-wide (D78b)."""
        if self._open >= limit:
            raise SubscriptionLimitError(f"all {limit} now-playing subscriptions are taken")
        state = self._channels.setdefault(channel, _Channel())
        mailbox = _Mailbox(self._elapsed, self._sleep, state.current)
        state.mailboxes.append(mailbox)
        self._open += 1
        return ListenerEvents(mailbox, partial(self._forget, channel, mailbox))

    def _tell(self, channel: BookmarkKey, deliver: Callable[[_Mailbox], None]) -> None:
        """Deliver to each subscription; one left too far behind is disconnected (D78b)."""
        state = self._channels.get(channel)
        if state is None:
            return
        for mailbox in tuple(state.mailboxes):
            deliver(mailbox)
            if mailbox.backlog > PENDING_LIMIT:
                self._forget(channel, mailbox)

    def _forget(self, channel: BookmarkKey, mailbox: _Mailbox) -> None:
        """A subscription is closed or disconnected: it ends and no longer counts."""
        if mailbox.is_shut:
            return
        mailbox.shut()
        self._open -= 1
        state = self._channels.get(channel)
        if state is not None:
            state.mailboxes.remove(mailbox)
            self._prune(channel, state)

    def _prune(self, channel: BookmarkKey, state: _Channel) -> None:
        if state.idle:
            self._channels.pop(channel, None)
