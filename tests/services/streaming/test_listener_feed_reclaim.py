"""A still-playing stream takes its feed back (spec D78c, from the F1 final review M1; D74 with
Q5: the channel is the bookmark key, two tabs share it, and the newest stream owns it, the
accepted Q5 recommendation; D78a: the status kinds, "ended" signed off and "stopped" dropped).

The scenario: tab 1 plays stream "a" on the channel; tab 2 tunes in on the same key, and its
stream "b" is admitted (``opened``), then ends (an engine failure, the listener leaving
mid-start, no broadcast, or a close). "a" still plays. D78c: "a"'s next title, and its close,
are told again; "b"'s end is still told on the channel.

These tests pin what is told, never how the feed decides it: a feed that hands the channel
back at once (an owner stack) and one that lets the right stream's next title take it back
both pass.

Design decisions, read from the spec (and the coordinator's rulings on the D78c audit):

- At the hand-back nothing more is told until "a"'s next title (D78c: "next title"): a page
  already subscribed was told "a"'s title once, and is not told it again.
- A page that subscribes after "b" ended and before "a"'s next title, whether it reconnects
  or another page stays subscribed, is NOT required to be told "a"'s title at once. D78c
  promises "a"'s *next* title, and ``opened("b")`` already cleared the channel's title. It may
  be told "a"'s current title (T3.5 and the F2 contract's "Replay": the current title while a
  stream plays), never a title "a" has moved past (T3.6); from "a"'s next title on, it is
  told like every other subscriber.
- "a"'s close is told whether or not a title came first: D78c says "its close" is told
  again, unconditionally, and the page learns a dropped stream ("stopped": reconnect) only
  from the feed (D78a).
- With more than one older stream still playing, the NEXT newest takes the channel back, not
  the oldest (D74 with Q5: the newest tab's titles win).
- A session the feed has been told has ended never takes the channel back: once ``ended``
  is told for a session, the feed holds nothing for it (no owner kept alive, no title told),
  and the channel goes once its pages leave.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest

from backend.services.streaming.bookmarks import BookmarkKey
from backend.services.streaming.listener_events import (
    ListenerEvents,
    ListenerFeed,
    NowPlaying,
    Status,
    StatusKind,
)
from tests.services.streaming.events_rig import GatedSleep, drain
from tests.services.streaming.helpers import Clock

STATION = UUID("0000c0de-0000-4000-8000-000000000001")
CHANNEL = BookmarkKey("car", STATION, 1995)
CAP = 10
DELAY = timedelta(seconds=1)
FERNANDO = NowPlaying(artist="ABBA", title="Fernando")
WATERLOO = NowPlaying(artist="ABBA", title="Waterloo")
RASPUTIN = NowPlaying(artist="Boney M.", title="Rasputin")

TUNING = Status(StatusKind.TUNING)
STOPPED = Status(StatusKind.STOPPED)
UNAVAILABLE = Status(StatusKind.UNAVAILABLE)

NEWER_ENDS = {
    "engine fails": StatusKind.UNAVAILABLE,
    "left mid-start": StatusKind.STOPPED,
    "no broadcast": StatusKind.NO_BROADCAST,
    "signed off": StatusKind.ENDED,
}
"""How the newer stream "b" ends, and the kind it is told with (D78a, D78b)."""

CLOSES = {"dropped": StatusKind.STOPPED, "signed off": StatusKind.ENDED}
"""How the older stream "a" closes later (D78a)."""

PAGES = ["another page stays", "no other page"]
"""Whether another page stays subscribed while the first page reconnects."""


@dataclass
class FeedRig:
    feed: ListenerFeed
    clock: Clock

    def title(self, session_id: str, title: NowPlaying) -> None:
        """``session_id``'s item starts now; its delay passes before anything else happens."""
        self.feed.title(CHANNEL, session_id, title, self.clock.now + DELAY)
        self.clock.advance(seconds=5)


@pytest.fixture
def rig() -> FeedRig:
    clock = Clock()
    return FeedRig(ListenerFeed(clock, GatedSleep(clock)), clock)


async def a_plays_then_b_ends(rig: FeedRig, newer_ends: StatusKind) -> ListenerEvents:
    """Tab 1's page is subscribed; "a" plays Fernando; "b" is admitted on the same key and
    ends. Returns tab 1's subscription, with everything told so far read and checked.

    Decided (coordinator ruling S3): at the hand-back nothing more is told until "a"'s next
    title (D78c: "next title"); the exact ``[TUNING, <b's end>]`` below pins that."""
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.title("a", FERNANDO)
    assert await drain(events) == [TUNING, FERNANDO]
    rig.feed.opened(CHANNEL, "b")
    rig.feed.ended(CHANNEL, "b", newer_ends)
    # D78c: the newer stream's end is still told on the channel
    assert await drain(events) == [TUNING, Status(newer_ends)]
    return events


@pytest.mark.parametrize("close", list(CLOSES.values()), ids=list(CLOSES))
@pytest.mark.parametrize("newer_ends", list(NEWER_ENDS.values()), ids=list(NEWER_ENDS))
async def test_the_older_streams_titles_and_close_are_told_after_a_newer_one_ends(
    rig: FeedRig, newer_ends: StatusKind, close: StatusKind
) -> None:
    # D78c: "the older stream's next title — and its close — are told again"
    events = await a_plays_then_b_ends(rig, newer_ends)
    rig.title("a", WATERLOO)
    assert await drain(events) == [WATERLOO]
    rig.title("a", RASPUTIN)  # every later title, not only the first after "b" ended
    assert await drain(events) == [RASPUTIN]
    rig.feed.ended(CHANNEL, "a", close)
    assert await drain(events) == [Status(close)]
    # Nothing after the close: a stale title for "a" is never told, and nothing is kept
    rig.title("a", FERNANDO)
    assert await drain(events) == []
    await events.aclose()
    assert rig.feed.channels == 0


@pytest.mark.parametrize("newer_ends", list(NEWER_ENDS.values()), ids=list(NEWER_ENDS))
async def test_the_older_streams_close_is_told_even_before_its_next_title(
    rig: FeedRig, newer_ends: StatusKind
) -> None:
    # D78c: "its close" is told again, whether or not a title came first; D78a: a page whose
    # audio dropped learns "stopped" (reconnect and resume) only from the feed
    events = await a_plays_then_b_ends(rig, newer_ends)
    rig.feed.ended(CHANNEL, "a", StatusKind.STOPPED)
    assert await drain(events) == [STOPPED]
    # Nothing after the close: a stale title for "a" is never told, and nothing is kept
    rig.title("a", WATERLOO)
    assert await drain(events) == []
    await events.aclose()
    assert rig.feed.channels == 0


@pytest.mark.parametrize("pages", PAGES)
async def test_a_reconnecting_page_is_told_the_older_streams_next_title(
    rig: FeedRig, pages: str
) -> None:
    # D78c with T3.5 and the F2 contract's "Replay". Decided: before "a"'s next title a
    # reconnected page is told nothing, or "a"'s current title, and nothing else. With no
    # other page, the channel has no subscriber at all between the drop and the reconnect.
    first = await a_plays_then_b_ends(rig, StatusKind.UNAVAILABLE)
    staying = first if pages == "another page stays" else None
    if staying is None:
        await first.aclose()  # the page drops ...
    reconnected = rig.feed.subscribe(CHANNEL, CAP)  # ... and reconnects
    assert await drain(reconnected) in ([], [FERNANDO])
    rig.title("a", WATERLOO)
    assert await drain(reconnected) == [WATERLOO]
    if staying is not None:
        assert await drain(staying) == [WATERLOO]
    rig.feed.ended(CHANNEL, "a", StatusKind.STOPPED)
    assert await drain(reconnected) == [STOPPED]
    if staying is not None:
        assert await drain(staying) == [STOPPED]


async def test_a_late_page_is_never_told_a_title_the_older_stream_has_moved_past(
    rig: FeedRig,
) -> None:
    # T3.6 (a late page is never told an old title) with D78c: "a" moves on to Waterloo while
    # "b" owns the channel (not told, T3.7); after "b" ends, a late page is told nothing, or
    # "a"'s current title, never Fernando, which has finished.
    staying = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.title("a", FERNANDO)
    rig.feed.opened(CHANNEL, "b")
    rig.title("a", WATERLOO)
    rig.feed.ended(CHANNEL, "b", StatusKind.UNAVAILABLE)
    assert await drain(staying) == [TUNING, FERNANDO, TUNING, UNAVAILABLE]
    late = rig.feed.subscribe(CHANNEL, CAP)
    assert await drain(late) in ([], [WATERLOO])


async def test_the_next_newest_still_playing_stream_takes_the_channel_back(
    rig: FeedRig,
) -> None:
    # D78c with D74 and Q5 (the newest tab's titles win; coordinator ruling S2): three streams
    # on one key; when the newest ends, the next newest takes the channel back, not the oldest
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.feed.opened(CHANNEL, "b")
    rig.feed.opened(CHANNEL, "c")
    rig.feed.ended(CHANNEL, "c", StatusKind.UNAVAILABLE)
    assert await drain(events) == [TUNING, TUNING, TUNING, UNAVAILABLE]
    rig.title("a", FERNANDO)
    rig.title("b", WATERLOO)
    assert await drain(events) == [WATERLOO]
    rig.feed.ended(CHANNEL, "b", StatusKind.STOPPED)
    rig.title("a", RASPUTIN)
    assert await drain(events) == [STOPPED, RASPUTIN]


async def test_a_stream_that_ended_before_the_newer_one_is_not_kept(rig: FeedRig) -> None:
    # D78c applies only "while an older stream on that key still plays": "a" closed while
    # "b" owned the channel (D74 with Q5: not told), so when "b" ends no stream is left. A
    # late page is told nothing, and the channel goes once its pages leave.
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.title("a", FERNANDO)
    rig.feed.opened(CHANNEL, "b")
    rig.feed.ended(CHANNEL, "a", StatusKind.STOPPED)
    rig.feed.ended(CHANNEL, "b", StatusKind.UNAVAILABLE)
    assert await drain(events) == [TUNING, FERNANDO, TUNING, UNAVAILABLE]
    late = rig.feed.subscribe(CHANNEL, CAP)
    assert await drain(late) == []
    await events.aclose()
    await late.aclose()
    assert rig.feed.channels == 0


async def test_a_session_told_ended_never_takes_the_channel_back(rig: FeedRig) -> None:
    # D78c: only a still-playing stream takes its feed back. The feed has been told "a"
    # ended, so a title reported for "a" afterwards (a stale report) is never told, and
    # nothing of "a" is kept for a late page.
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.title("a", FERNANDO)
    rig.feed.opened(CHANNEL, "b")
    rig.feed.ended(CHANNEL, "a", StatusKind.STOPPED)
    rig.feed.ended(CHANNEL, "b", StatusKind.UNAVAILABLE)
    assert await drain(events) == [TUNING, FERNANDO, TUNING, UNAVAILABLE]
    rig.title("a", WATERLOO)
    assert await drain(events) == []
    late = rig.feed.subscribe(CHANNEL, CAP)
    assert await drain(late) == []
