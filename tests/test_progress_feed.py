"""The shared ``/ws`` progress feed (``backend.websocket.ProgressFeed``).

Requirements:
- one poll per interval serves every connected client (not one poll per client);
- no client, no poll;
- a new client's first frame comes from a poll that started after it connected;
- a client that falls behind gets the newest frame, not a backlog;
- a failed poll closes clients with 1013 (pool saturated) or 1011 (database error), and the
  feed keeps polling for the clients that connect next.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from backend.websocket import FeedClosed, Frame, ProgressFeed

INTERVAL_S = 0.05


class CountingPoll:
    """Frames numbered by poll; a poll can be held open, or made to fail once."""

    def __init__(self) -> None:
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.fail_with: BaseException | None = None

    async def __call__(self) -> Frame:
        self.calls += 1
        number = self.calls
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error
        return {"tasks": [{"poll": number}]}


@pytest.fixture()
def poll() -> CountingPoll:
    return CountingPoll()


@pytest.fixture()
async def feed(poll: CountingPoll) -> AsyncIterator[ProgressFeed]:
    feed = ProgressFeed(poll, INTERVAL_S)
    feed.start()
    yield feed
    await feed.stop()


def _poll_number(item: Frame | FeedClosed) -> int:
    assert not isinstance(item, FeedClosed)
    number: int = item["tasks"][0]["poll"]
    return number


async def test_no_client_no_poll(feed: ProgressFeed, poll: CountingPoll) -> None:
    await asyncio.sleep(INTERVAL_S * 4)

    assert poll.calls == 0


async def test_one_poll_serves_every_client(feed: ProgressFeed, poll: CountingPoll) -> None:
    async with feed.subscription() as first, feed.subscription() as second:
        a = [_poll_number(await first.get()) for _ in range(3)]
        b = [_poll_number(await second.get()) for _ in range(3)]
        polls = poll.calls

    # Three frames each, but nowhere near six polls: both clients read the same polls.
    assert polls <= 5
    assert set(a) & set(b)


async def test_first_frame_is_polled_after_connecting(
    feed: ProgressFeed, poll: CountingPoll
) -> None:
    async with feed.subscription() as early:
        await early.get()
        poll.gate = asyncio.Event()
        while poll.calls < 2:  # poll 2 is in flight, held open
            await asyncio.sleep(0)
        async with feed.subscription() as late:
            poll.gate.set()
            poll.gate = None
            first = _poll_number(await late.get())

    assert first >= 3


async def test_a_slow_client_gets_the_newest_frame(feed: ProgressFeed, poll: CountingPoll) -> None:
    async with feed.subscription() as items:
        await asyncio.sleep(INTERVAL_S * 6)
        polls_so_far = poll.calls
        newest = _poll_number(await items.get())

    assert polls_so_far >= 3
    assert newest >= polls_so_far - 1
    assert items.empty()


@pytest.mark.parametrize(
    ("error", "code"),
    [(PoolTimeout("saturated"), 1013), (psycopg.OperationalError("connection lost"), 1011)],
)
async def test_a_failed_poll_closes_clients_and_the_feed_carries_on(
    feed: ProgressFeed, poll: CountingPoll, error: BaseException, code: int
) -> None:
    poll.fail_with = error
    async with feed.subscription() as items:
        closed = await items.get()

    assert isinstance(closed, FeedClosed)
    assert closed.code == code

    async with feed.subscription() as items:
        assert _poll_number(await items.get()) > 1
