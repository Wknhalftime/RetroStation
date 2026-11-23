"""The cache fill's request budget (phase 4.1 PR 2b; SPEC S8–S11, E6; D9, D10, D11; AUD-R093),
on PostgreSQL through ``cache_fill_rig``.

PR 2's gate (part B) sent 4,218 requests for 2,276 calls in 114 minutes: the shared client tries
a call 3 times on a 503, MusicBrainz refused 61% of requests, and the cut-off never fired (D9).
In that run a first try made 10–12 s after MusicBrainz's last answer was refused 1% of the time,
against about half within 10 s. Lance ruled D10, "One try + 10 s pause", and D11, "2 hours":

- **One request a call** (S8): the fill asks through a one-attempt view of the client, so a
  refused call is one request and its key waits for the next run.
- **The pause** (S9): after a refusal (any transient failure: a 429 or 5xx, a timeout, a network
  error) the fill waits 10 s before its next request, on the batch's clock, so the pause counts
  inside the batch's 110 s. An answer or a permanent error (M5) is followed by no pause, and
  nothing waits when no request follows: the run's last call, or a cut-off.
- **The 2-hour limit** (S10, E6): a run hands on no batch once 120 minutes have passed since it
  started (``started_at``, on the wall clock), and a batch that starts after that, as one does
  after a worker restart, makes no call. Either way the run ends there, its last row says so
  as D8's cut-off does ("payoff, stopped (2 hours up)"), the names its tiers filled are
  re-checked (S7), and the rest waits for the next run.
- **The report** (S11): once a tier has failed calls, the detail counts them ("payoff, 2 of 4
  (cap 2,600), 1 failed"), while D4's and D8's pinned strings hold. A batch's last row carries
  ``requests`` (every attempt the run's client made), ``minutes`` (since ``started_at``) and
  ``ended`` (``done``, ``cut_off`` or ``time``; none while the run hands on). The run's last row,
  when calls failed, also carries ``warning: "mb_fill_calls_failed"`` and ``failed_calls``.

The rig steps its clock for a pause instead of sleeping (``MbServer.pauses``: the requests made
before each pause, and its seconds). The wall clock is the task module's ``utc_now``, set here
to follow the rig's clock from an offset.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.tasks import cache_fill_tasks
from tests.integration.cache_fill_rig import (
    Conn,
    Fill,
    Name,
    cache_keys,
    catalog_artist,
    key,
    library_file,
    limits,
    line,
    log_name,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 23, 9, tzinfo=UTC)
NATO = ("ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL", "INDIA", "JULIET")
TWO_HOURS = timedelta(minutes=120)
PAUSE = 10.0


@dataclass
class Wall:
    """The wall clock the fill reads: T0, plus an offset a test moves, plus the rig's clock."""

    fill: Fill
    offset: timedelta = timedelta(0)

    def now(self) -> datetime:
        return T0 + self.offset + timedelta(seconds=self.fill.server.clock)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def fill(conn: Conn, migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fill]:
    rig = Fill(conn, migrated_db, monkeypatch)
    yield rig
    rig.close()


@pytest.fixture
def wall(fill: Fill, monkeypatch: pytest.MonkeyPatch) -> Wall:
    clock = Wall(fill)
    monkeypatch.setattr(cache_fill_tasks, "utc_now", clock.now)
    return clock


def payoff(conn: Conn, original: str, title: str, plays: int) -> Name:
    """A payoff pair: *original* played *title* *plays* times, and one act's file holds it."""
    name = log_name(conn, original)
    line(conn, name, title, plays=plays)
    library_file(conn, title, mbids=[str(uuid4())])
    return name


def payoffs(conn: Conn, count: int) -> list[Name]:
    """*count* payoff pairs, ALPHA's "Song 1" the most played."""
    return [payoff(conn, NATO[n], f"Song {n + 1}", 100 - n) for n in range(count)]


def songs(count: int) -> list[str]:
    return [f"Song {n + 1}" for n in range(count)]


# --- S8: one request a call ---------------------------------------------------------------------


def test_each_call_asks_once(conn: Conn, fill: Fill) -> None:
    """S8 (D10): a refused call is one request, not the client's three; nothing is cached for it,
    and the next run asks it again."""
    payoffs(conn, 3)
    fill.server.fail = {"Song 2": "503"}

    fill.run()

    assert fill.server.titles() == songs(3)
    assert cache_keys(conn, "recording-name-title:") == {
        key("ALPHA", "Song 1"),
        key("CHARLIE", "Song 3"),
    }
    fill.server.fail = {}
    fill.server.requests.clear()
    fill.run()
    assert fill.server.titles() == ["Song 2"]


def test_an_alias_request_is_asked_once_too(conn: Conn, fill: Fill) -> None:
    """S8: the alias step's request is a fill call like any other: one request, refused or not."""
    mbid = "a3b2c1d0-0000-4000-8000-000000000040"
    catalog_artist(conn, "Boston", mbid)
    fill.server.fail = {mbid: "503"}

    fill.run()

    assert len(fill.server.asked("aliases")) == 1


# --- S9: the pause after a refusal --------------------------------------------------------------


@pytest.mark.parametrize("refusal", ["503", "timeout", "connect"])
def test_a_refusal_pauses_the_next_request(conn: Conn, fill: Fill, refusal: str) -> None:
    """S9: after a refused call the fill waits 10 s before its next request; an answer is
    followed by none."""
    payoffs(conn, 4)
    fill.server.script = [None, refusal, None, None]

    fill.run()

    assert fill.server.titles() == songs(4)
    assert fill.server.pauses == [(2, PAUSE)]


def test_a_permanent_error_pauses_nothing(conn: Conn, fill: Fill) -> None:
    """S9, M5: a 4xx other than 429 skips its item for the run and is no refusal: no pause."""
    payoffs(conn, 2)
    fill.server.script = ["400", None]

    fill.run()

    assert fill.server.titles() == songs(2)
    assert fill.server.pauses == []


def test_nothing_waits_when_no_request_follows(conn: Conn, fill: Fill) -> None:
    """S9: the run's last call, refused, waits for nothing; nor does a cut-off's fifth refusal."""
    payoffs(conn, 1)
    fill.server.down = "503"
    fill.run()
    assert fill.server.pauses == []

    for n in range(1, 7):
        payoff(conn, NATO[n], f"Song {n + 1}", 50 - n)
    fill.server.requests.clear()
    fill.run()
    assert len(fill.server.requests) == 5  # cut off at the fifth refusal in a row (E1)
    assert fill.server.pauses == [(n, PAUSE) for n in (1, 2, 3, 4)]


def test_the_pause_counts_inside_the_batchs_110_seconds(conn: Conn, fill: Fill) -> None:
    """S9 (R9): the pause runs on the batch's clock, so a batch of refusals and pauses still
    starts no call once 110 s have passed. Calls take 10 s here: refusals start at 0, 30, 60 and
    90 s and answers at 20, 50 and 80 s, then the fourth pause ends at 110 s. Without the pauses
    all nine calls would have fit in the batch."""
    payoffs(conn, 9)
    fill.server.seconds_per_call = 10.0
    fill.server.script = ["503", None] * 5

    fill.start()
    fill.batch()

    assert len(fill.server.requests) == 7
    assert fill.server.pauses == [(n, PAUSE) for n in (1, 3, 5, 7)]
    assert len(fill.batches_waiting()) == 1
    while fill.batch():
        pass
    assert fill.server.titles() == songs(9)
    assert fill.server.pauses == [(n, PAUSE) for n in (1, 3, 5, 7)]  # the run's last call: none


# --- S10, E6: the 2-hour limit ------------------------------------------------------------------


def test_a_run_hands_on_no_batch_once_two_hours_have_passed(
    conn: Conn, fill: Fill, wall: Wall
) -> None:
    """S10 (D11): a batch that ends 120 minutes or more after its run started hands on no batch.
    The run ends ``time``, the names its tier filled are re-checked (S7), and the rest waits for
    the next run."""
    fill.use(limits(batch_calls=2))
    names = payoffs(conn, 6)

    fill.start()
    fill.batch()
    first = fill.last()
    assert first["ended"] is None and first["next_batch"] is True
    # The second batch starts at 119:59 into the run (its two calls take 2 s) and ends at 120:01.
    wall.offset = TWO_HOURS - timedelta(seconds=1) - timedelta(seconds=fill.server.clock)
    fill.batch()

    assert fill.batches_waiting() == []
    assert fill.server.titles() == songs(4)
    last = fill.last()
    assert last["status"] == "completed"
    assert last["ended"] == "time"
    assert last["detail"] == "payoff, stopped (2 hours up)"
    assert last["next_batch"] is False
    assert last["cut_off"] is False
    assert last["minutes"] == 120.0
    assert fill.rechecks() == [sorted(n.normalized for n in names[:4])]

    fill.server.requests.clear()
    fill.run()
    assert fill.server.titles() == ["Song 5", "Song 6"]


def test_a_batch_picked_up_after_the_two_hours_makes_no_call(
    conn: Conn, fill: Fill, wall: Wall
) -> None:
    """E6 (D11): the worker was down, and the run's next batch runs three hours after the run
    started. It makes no call and ends the run ``time``; the names already filled are re-checked,
    and the rest waits for the next run."""
    fill.use(limits(batch_calls=2))
    names = payoffs(conn, 4)

    fill.start()
    fill.batch()
    wall.offset = timedelta(hours=3)
    fill.batch()

    assert len(fill.server.requests) == 2
    assert fill.batches_waiting() == []
    last = fill.last()
    assert last["status"] == "completed"
    assert last["ended"] == "time"
    assert last["detail"] == "payoff, stopped (2 hours up)"
    assert last["next_batch"] is False
    assert fill.rechecks() == [sorted(n.normalized for n in names[:2])]


def test_a_run_inside_its_two_hours_goes_on(conn: Conn, fill: Fill, wall: Wall) -> None:
    """S10: at 119 minutes the run still hands on its next batch."""
    fill.use(limits(batch_calls=2))
    payoffs(conn, 4)

    fill.start()
    fill.batch()
    wall.offset = TWO_HOURS - timedelta(minutes=1)
    fill.batch()

    assert fill.server.titles() == songs(4)
    assert fill.last()["ended"] == "done"


# --- S11: the report ----------------------------------------------------------------------------


def test_the_detail_counts_failures_once_there_are_any(conn: Conn, fill: Fill) -> None:
    """S11 (D10, 4a): the detail adds the tier's failed calls once there are any; with none it is
    D4's, unchanged."""
    payoffs(conn, 4)
    fill.server.script = [None, "503", None, None]

    fill.run()

    assert [seen.progress["detail"] for seen in fill.server.seen if seen.progress] == [
        "payoff, 0 of 4 (cap 2,600)",
        "payoff, 1 of 4 (cap 2,600)",
        "payoff, 2 of 4 (cap 2,600), 1 failed",
        "payoff, 3 of 4 (cap 2,600), 1 failed",
    ]


def test_the_last_row_reports_requests_minutes_and_how_the_run_ended(
    conn: Conn, fill: Fill, wall: Wall
) -> None:
    """S11 (D10, 4b): the run's last row carries its requests, its minutes since it started, how
    it ended, and, since a call failed, the warning the bar shows in amber."""
    payoffs(conn, 3)
    fill.server.script = [None, "503", None]

    fill.run()

    last = fill.last()
    assert last["requests"] == len(fill.server.requests) == 3
    assert last["minutes"] == 0.2  # 3 calls of 1 s and one 10 s pause: 13 s
    assert last["ended"] == "done"
    assert last["warning"] == "mb_fill_calls_failed"
    assert last["failed_calls"] == 1


def test_a_run_with_no_failed_call_carries_no_warning(conn: Conn, fill: Fill) -> None:
    """S11: the warning is for failed calls only."""
    payoffs(conn, 2)

    fill.run()

    last = fill.last()
    assert last["ended"] == "done"
    assert last["requests"] == 2
    assert "warning" not in last
    assert "failed_calls" not in last


def test_a_cut_off_run_reports_its_failures_beside_d8s_words(conn: Conn, fill: Fill) -> None:
    """S11, D8: a cut-off keeps D8's detail exactly; the warning counts the failed calls."""
    payoffs(conn, 7)
    fill.server.down = "503"

    fill.run()

    last = fill.last()
    assert last["detail"] == "payoff, stopped (MusicBrainz not answering)"
    assert last["ended"] == "cut_off"
    assert last["requests"] == 5
    assert last["warning"] == "mb_fill_calls_failed"
    assert last["failed_calls"] == 5
