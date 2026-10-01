"""The listener feed: who is told what, per channel (spec D13: now-playing backend -> browser;
D73: artist and title only; D74: the channel is the bookmark key, two tabs share it, and the
newest stream owns it (the accepted Q5 recommendation); D78a: the stream's status kinds;
D78b (provisional): an app-wide subscription cap, 16 pending events per subscription, and a
refusal never replaces the owner's pending title; contract: "starts the now-playing timer
(~1 s delay)"; D47: the delay is on the elapsed clock)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest

from backend.services.streaming.bookmarks import BookmarkKey
from backend.services.streaming.errors import SubscriptionLimitError
from backend.services.streaming.listener_events import (
    ListenerFeed,
    NowPlaying,
    Status,
    StatusKind,
)
from tests.services.streaming.events_rig import GatedSleep, drain, has_ended, next_event
from tests.services.streaming.helpers import Clock

STATION = UUID("0000c0de-0000-4000-8000-000000000001")
OTHER_STATION = UUID("0000c0de-0000-4000-8000-000000000002")
CHANNEL = BookmarkKey("car", STATION, 1995)
CAP = 10
DELAY = timedelta(seconds=1)
FERNANDO = NowPlaying(artist="ABBA", title="Fernando")
WATERLOO = NowPlaying(artist="ABBA", title="Waterloo")

TUNING = Status(StatusKind.TUNING)
STOPPED = Status(StatusKind.STOPPED)
ENDED = Status(StatusKind.ENDED)
BUSY = Status(StatusKind.BUSY)


@dataclass
class FeedRig:
    feed: ListenerFeed
    clock: Clock
    sleep: GatedSleep

    def title(self, session_id: str, title: NowPlaying, channel: BookmarkKey = CHANNEL) -> None:
        """``session_id``'s item starts now: its title shows one delay later."""
        self.feed.title(channel, session_id, title, self.clock.now + DELAY)


@pytest.fixture
def rig() -> FeedRig:
    clock = Clock()
    sleep = GatedSleep(clock)
    return FeedRig(ListenerFeed(clock, sleep), clock, sleep)


async def test_a_subscriber_is_told_the_streams_statuses_in_order(rig: FeedRig) -> None:
    # D78a: the kinds, in the order they happen; D13
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.feed.ended(CHANNEL, "a", StatusKind.STOPPED)
    assert await drain(events) == [TUNING, STOPPED]


async def test_a_title_is_told_once_its_delay_has_passed(rig: FeedRig) -> None:
    # Contract "~1 s delay"; D47: the wait is the injected sleep, on the elapsed clock
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    assert await next_event(events) == TUNING
    rig.title("a", FERNANDO)
    waiting = asyncio.ensure_future(next_event(events))
    await rig.sleep.wait_pending()
    assert not waiting.done()
    assert rig.sleep.asked == [1.0]
    rig.sleep.release()
    assert await waiting == FERNANDO


async def test_a_title_superseded_within_its_delay_is_never_told(rig: FeedRig) -> None:
    # Contract "~1 s delay": a quick skip is never shown
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    assert await next_event(events) == TUNING
    rig.title("a", FERNANDO)
    assert await drain(events) == []
    rig.clock.advance(seconds=0.5)
    rig.title("a", WATERLOO)
    waiting = asyncio.ensure_future(next_event(events))
    await rig.sleep.wait_pending()
    rig.sleep.release()
    assert await waiting == WATERLOO
    assert await drain(events) == []


async def test_a_stop_during_a_titles_delay_is_told_at_once_and_the_title_dropped(
    rig: FeedRig,
) -> None:
    # D78a: "stopped"; D13
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    assert await next_event(events) == TUNING
    rig.title("a", FERNANDO)
    waiting = asyncio.ensure_future(next_event(events))
    await rig.sleep.wait_pending()
    rig.feed.ended(CHANNEL, "a", StatusKind.STOPPED)
    assert await waiting == STOPPED  # no release: told at once
    rig.clock.advance(seconds=5)
    assert await drain(events) == []
    assert rig.sleep.pending == 0


async def test_a_subscriber_joining_mid_song_is_told_the_current_title_first(
    rig: FeedRig,
) -> None:
    # D13: a page that connects (or reconnects) mid-song is told what is playing
    rig.feed.opened(CHANNEL, "a")
    rig.title("a", FERNANDO)
    rig.clock.advance(seconds=30)
    events = rig.feed.subscribe(CHANNEL, CAP)
    assert await next_event(events) == FERNANDO
    assert rig.sleep.pending == 0  # told at once, with no wait


@pytest.mark.parametrize(
    "then", ["the stream ended", "a newer stream opened"], ids=["ended", "newer stream"]
)
async def test_a_late_subscriber_is_never_told_an_old_title(rig: FeedRig, then: str) -> None:
    # D78a: statuses are live, never replayed; design note 8 and the F2 contract: a page is
    # replayed the current title only while that stream plays. Another tab stays subscribed
    # throughout, so the channel itself lives on.
    staying = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.title("a", FERNANDO)
    rig.clock.advance(seconds=30)
    if then == "the stream ended":
        rig.feed.ended(CHANNEL, "a", StatusKind.ENDED)
    else:
        rig.feed.opened(CHANNEL, "b")
    late = rig.feed.subscribe(CHANNEL, CAP)
    assert await drain(late) == []
    await staying.aclose()


async def test_the_newest_stream_owns_the_channel(rig: FeedRig) -> None:
    # D74 with Q5: two tabs on one key; the newest stream's events win
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.feed.opened(CHANNEL, "b")
    rig.title("a", FERNANDO)
    rig.clock.advance(seconds=5)
    rig.feed.ended(CHANNEL, "a", StatusKind.STOPPED)
    assert await drain(events) == [TUNING, TUNING]
    rig.title("b", WATERLOO)
    rig.clock.advance(seconds=5)
    rig.feed.ended(CHANNEL, "b", StatusKind.ENDED)
    assert await drain(events) == [WATERLOO, ENDED]


async def test_a_refused_tune_in_is_told_and_leaves_the_stream_playing(rig: FeedRig) -> None:
    # D78a: "busy" is a refusal; D74: it does not change who owns the channel
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.feed.refused(CHANNEL, StatusKind.BUSY)
    rig.title("a", FERNANDO)
    rig.clock.advance(seconds=5)
    assert await drain(events) == [TUNING, BUSY, FERNANDO]


@pytest.mark.parametrize(
    "elsewhere",
    [
        BookmarkKey("phone", STATION, 1995),
        BookmarkKey("car", OTHER_STATION, 1995),
        BookmarkKey("car", STATION, 1996),
    ],
    ids=["key", "station", "year"],
)
async def test_channels_are_separate_per_key_station_and_year(
    rig: FeedRig, elsewhere: BookmarkKey
) -> None:
    # D74: the channel is the bookmark key (key, station, year)
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(elsewhere, "b")
    rig.feed.refused(elsewhere, StatusKind.BUSY)
    rig.title("b", FERNANDO, channel=elsewhere)
    rig.clock.advance(seconds=5)
    rig.feed.ended(elsewhere, "b", StatusKind.STOPPED)
    assert await drain(events) == []


async def test_every_subscriber_of_a_channel_is_told(rig: FeedRig) -> None:
    # D74: two tabs share the key, so both are told
    first = rig.feed.subscribe(CHANNEL, CAP)
    second = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    rig.title("a", FERNANDO)
    rig.clock.advance(seconds=5)
    assert await drain(first) == [TUNING, FERNANDO]
    assert await drain(second) == [TUNING, FERNANDO]


async def test_a_subscriber_that_has_left_is_forgotten(rig: FeedRig) -> None:
    # No leak: a closed subscription ends, and the channel goes once its stream ends too
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    await events.aclose()
    assert await has_ended(events)
    rig.title("a", FERNANDO)
    rig.feed.refused(CHANNEL, StatusKind.BUSY)
    assert rig.feed.channels == 1  # the stream still owns it
    rig.feed.ended(CHANNEL, "a", StatusKind.STOPPED)
    assert rig.feed.channels == 0


def test_status_kinds_are_the_contracts_names() -> None:
    # D78a: the six kinds, as F2 reads them
    assert sorted(kind.value for kind in StatusKind) == sorted(
        ["tuning", "no_broadcast", "busy", "unavailable", "ended", "stopped"]
    )


async def test_a_refusal_never_replaces_the_owners_pending_title(rig: FeedRig) -> None:
    # D78b: another tab's refused attempt never hides the playing song (review I5)
    events = rig.feed.subscribe(CHANNEL, CAP)
    rig.feed.opened(CHANNEL, "a")
    assert await next_event(events) == TUNING
    rig.title("a", FERNANDO)
    waiting = asyncio.ensure_future(next_event(events))
    await rig.sleep.wait_pending()
    rig.feed.refused(CHANNEL, StatusKind.BUSY)
    assert await waiting == BUSY  # at once, with no release
    title = asyncio.ensure_future(next_event(events))
    await rig.sleep.wait_pending()
    rig.sleep.release()
    assert await title == FERNANDO


@pytest.mark.parametrize(("refusals", "kept"), [(16, True), (17, False)], ids=["16", "17"])
async def test_a_subscription_holds_at_most_16_pending_events(
    rig: FeedRig, refusals: int, kept: bool
) -> None:
    # D78b: 16 pending events per subscription; a subscriber that falls further behind is
    # disconnected (at most 16 are told, then it ends), and its place under the cap is free
    # again. Whether the held events are dropped at once is left open (D78b: "disconnected").
    events = rig.feed.subscribe(CHANNEL, 1)
    kinds = [StatusKind.BUSY if n % 2 == 0 else StatusKind.UNAVAILABLE for n in range(refusals)]
    for kind in kinds:
        rig.feed.refused(CHANNEL, kind)
    if kept:
        assert await drain(events) == [Status(kind) for kind in kinds]
        assert not await has_ended(events)
        return
    told = await drain(events)
    assert len(told) <= 16
    assert await has_ended(events)
    assert rig.feed.channels == 0
    rig.feed.subscribe(CHANNEL, 1)  # the disconnected subscription no longer counts


async def test_subscriptions_beyond_the_cap_are_refused_until_one_closes(rig: FeedRig) -> None:
    # D78b: the cap is app-wide, across channels
    first = rig.feed.subscribe(CHANNEL, 2)
    rig.feed.subscribe(BookmarkKey("phone", STATION, 1995), 2)
    with pytest.raises(SubscriptionLimitError):
        rig.feed.subscribe(BookmarkKey("car", OTHER_STATION, 2001), 2)
    await first.aclose()
    rig.feed.subscribe(BookmarkKey("car", OTHER_STATION, 2001), 2)
