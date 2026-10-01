"""The stream service hands the feed back to a still-playing stream (spec D78c, from the F1
final review M1; D74 with Q5: one resume key per browser, two tabs share it, and the newest
stream owns the channel, the accepted Q5 recommendation; D78a: "ended" signed off, "stopped"
dropped; D78b: an unexpected failure is "unavailable"; D6: a listener who leaves mid-start).

The scenario: tab 1 plays the older stream on key "car"; tab 2 tunes in on the same key, and
its newer stream is admitted, then ends. The older stream still plays. D78c: its next title,
and its close, are told again; the newer stream's end is still told on the channel.

These tests pin what a page is told, never how the service decides it. Design decisions
(read from the spec, with the coordinator's rulings on the D78c audit):

- At the hand-back nothing more is told until the older stream's next title (D78c: "next
  title"): a page already subscribed was told its title once, and is not told it again.
- A page that subscribes after the newer stream ended and before the older one's next title,
  whether it reconnects or another page stays subscribed, is NOT required to be told a title
  at once: D78c promises the older stream's *next* title. It may be told the older stream's
  current title (T3.5 and the F2 contract's "Replay"), and nothing else.
- The older stream's close is told whether or not one of its titles came first (D78c: "its
  close" is told again; D78a: a page learns "stopped" only from the feed).
- D78c says "a newer stream ... ends"; a failed tune-in is its example, not its limit. So a
  newer stream that played and then closed hands the channel back too.
- With more than one older stream still playing, the next newest takes the channel back (D74
  with Q5); this is pinned at feed level (``test_listener_feed_reclaim.py``).
- A stream that closed while the newer one owned the channel is not playing, so D78c does not
  apply to it: when the newer stream ends, nothing is kept for it. Nothing is kept after the
  older stream's own close either: the channel goes once its pages leave.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.domain.streaming import NoBroadcastError
from backend.services.streaming.errors import StreamUnavailableError
from backend.services.streaming.listener_events import (
    ListenerEvent,
    ListenerEvents,
    NowPlaying,
    Status,
    StatusKind,
)
from tests.services.streaming.events_rig import EventsRig, drain, make_events_rig
from tests.services.streaming.helpers import DAY, STATION, song

TUNING = Status(StatusKind.TUNING)
STOPPED = Status(StatusKind.STOPPED)
FERNANDO = NowPlaying(artist="ABBA", title="Fernando")
WATERLOO = NowPlaying(artist="ABBA", title="Waterloo")


@pytest.fixture
def rig(tmp_path: Path) -> EventsRig:
    return make_events_rig(tmp_path)


def morning(rig: EventsRig) -> None:
    """06:00 Fernando (200 s) and 06:03:20 Waterloo, then more. NOW is 06:01."""
    rig.schedule.set_day(
        STATION,
        DAY,
        [
            song("06:00:00", title="Fernando", artist="ABBA"),
            song("06:03:20", title="Waterloo", artist="ABBA"),
            song("06:06:40", title="Rasputin", artist="Boney M."),
        ],
    )


async def play(rig: EventsRig, session_id: str, seq: int) -> None:
    """``seq`` starts on ``session_id``, and its title's delay passes."""
    await rig.item(session_id, seq)
    rig.started(session_id, seq)
    rig.elapse(1)


async def tune_in_while_starting(rig: EventsRig) -> asyncio.Task[str]:
    """Tab 2 tunes in on "car"; its engine is still starting when this returns."""
    rig.engines.hold = asyncio.Event().wait  # never ready
    rig.engines.start_seen.clear()
    opening = rig.open_task("car")
    await asyncio.wait_for(rig.engines.start_seen.wait(), timeout=2.0)
    return opening


async def leave_mid_start(opening: asyncio.Task[str]) -> None:
    opening.cancel()
    await asyncio.wait({opening})
    assert opening.cancelled()


# ---- how the newer stream ends ---------------------------------------------------------------


async def _engine_fails(rig: EventsRig) -> None:
    rig.engines.fail = True
    with pytest.raises(StreamUnavailableError):
        await rig.open("car")


async def _left_mid_start(rig: EventsRig) -> None:
    await leave_mid_start(await tune_in_while_starting(rig))


async def _no_broadcast(rig: EventsRig) -> None:
    # The older stream has read its day already: each day is read once per session (carried
    # item C9, service.py). If that ever changes, this case fails for a reason outside D78c.
    rig.schedule.set_day(STATION, DAY, [song("10:00:00")])
    with pytest.raises(NoBroadcastError):
        await rig.open("car")


async def _played_then_closed(rig: EventsRig) -> None:
    newer = await rig.open("car")
    await play(rig, newer, 0)
    rig.service.close(newer)


@dataclass(frozen=True)
class Newer:
    """How tab 2's newer stream ends, and what the channel is told after its ``tuning``."""

    ends: Callable[[EventsRig], Awaitable[None]]
    told: list[ListenerEvent]


NEWER = {
    "engine fails": Newer(_engine_fails, [Status(StatusKind.UNAVAILABLE)]),
    "left mid-start": Newer(_left_mid_start, [STOPPED]),
    "no broadcast": Newer(_no_broadcast, [Status(StatusKind.NO_BROADCAST)]),
    "played then closed": Newer(_played_then_closed, [FERNANDO, STOPPED]),
}


async def older_plays_then_newer_ends(rig: EventsRig, newer: Newer) -> tuple[str, ListenerEvents]:
    """Tab 1's page subscribes and its stream plays Fernando; tab 2's stream on the same key is
    admitted and ends. Returns the older stream and tab 1's subscription, read so far.

    Decided (coordinator ruling S3): at the hand-back nothing more is told until the older
    stream's next title (D78c: "next title"); the exact ``[TUNING, *newer.told]`` below pins
    that."""
    morning(rig)
    events = await rig.subscribe("car")
    older = await rig.open("car")
    await play(rig, older, 0)
    assert await drain(events) == [TUNING, FERNANDO]
    await newer.ends(rig)
    # D78c: the newer stream's end is still told on the channel
    assert await drain(events) == [TUNING, *newer.told]
    return older, events


# ---- D78c -------------------------------------------------------------------------------------


@pytest.mark.parametrize("newer", list(NEWER.values()), ids=list(NEWER))
async def test_the_older_streams_next_title_and_close_are_told_after_a_newer_one_ends(
    rig: EventsRig, newer: Newer
) -> None:
    # D78c: "the older stream's next title — and its close — are told again"
    older, events = await older_plays_then_newer_ends(rig, newer)
    await play(rig, older, 1)
    assert await drain(events) == [WATERLOO]
    rig.service.close(older)
    assert await drain(events) == [STOPPED]
    await events.aclose()  # nothing is kept after the close
    assert rig.service.event_channels == 0


@pytest.mark.parametrize("newer", list(NEWER.values()), ids=list(NEWER))
async def test_the_older_streams_close_is_told_even_before_its_next_title(
    rig: EventsRig, newer: Newer
) -> None:
    # D78c: "its close" is told again; tab 1 is closed mid-song, before its next title
    older, events = await older_plays_then_newer_ends(rig, newer)
    rig.service.close(older)
    assert await drain(events) == [STOPPED]
    await events.aclose()  # nothing is kept after the close
    assert rig.service.event_channels == 0


@pytest.mark.parametrize("pages", ["another page stays", "no other page"])
async def test_a_page_reconnecting_after_the_newer_stream_failed_is_told_the_next_title(
    rig: EventsRig, pages: str
) -> None:
    # D78c with T3.5 and the F2 contract's "Replay": tab 1's page reloads after tab 2's
    # tune-in failed. Before the older stream's next title it is told nothing, or that
    # stream's current title, and nothing else; then its next title and its close. With no
    # other page, the channel has no subscriber at all between the drop and the reconnect.
    older, first = await older_plays_then_newer_ends(rig, NEWER["engine fails"])
    staying = first if pages == "another page stays" else None
    if staying is None:
        await first.aclose()  # the page drops ...
    reconnected = await rig.subscribe("car")  # ... and reconnects
    assert await drain(reconnected) in ([], [FERNANDO])
    await play(rig, older, 1)
    assert await drain(reconnected) == [WATERLOO]
    if staying is not None:
        assert await drain(staying) == [WATERLOO]
    rig.service.close(older)
    assert await drain(reconnected) == [STOPPED]


async def test_a_stream_closed_while_the_newer_one_owned_the_channel_is_not_kept(
    rig: EventsRig,
) -> None:
    # D78c applies "while an older stream on that key still plays". Tab 1 closes while tab
    # 2's stream owns the channel (D74 with Q5: not told), then tab 2 leaves mid-start. No
    # stream is left: a late page is told nothing, and the channel goes once its pages leave.
    morning(rig)
    events = await rig.subscribe("car")
    older = await rig.open("car")
    await play(rig, older, 0)
    opening = await tune_in_while_starting(rig)
    rig.service.close(older)
    await leave_mid_start(opening)
    assert await drain(events) == [TUNING, FERNANDO, TUNING, STOPPED]
    late = await rig.subscribe("car")
    assert await drain(late) == []
    await events.aclose()
    await late.aclose()
    assert rig.service.event_channels == 0
