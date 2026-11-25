"""The pause at a run's end (phase 4.1 PR 2c; SPEC S9; D10, D11, D18; AUD-R094, refs AUD-R093).

S9: after a refusal the fill waits 10 s before its next request, and "no pause follows ... the
run's last call". PR 3's gate run (D14, 2026-11-24) found a refusal that no request follows that
did not end its batch: in the run's last batch, once its last 10 s had begun with the run's
minutes up after the pause, ``_Batch.request_follows`` was False, so no pause, and ``run_batch``
called on at once until 110 s (two requests, at 22:08:01 and 22:08:03). Lance ruled D18, "Fix
it": a refusal that no request follows ends the batch there, so the refused call is the run's
last call: no pause and no further call.

The run's 120 minutes can also end inside the pause: the refusal lands at 119:55 and its pause
would end at 120:05. No request follows it then either, so the run hands on no batch and ends
``time`` (the coordinator's reading of D18's "the run's last call", for Lance: otherwise the next
batch starts before 120:00 and asks at once, with no pause).

``run_batch`` is driven with fake ports at the real limits (100 calls, 110 s, a 10 s pause, 120
minutes): time moves only while MusicBrainz answers a call (``seconds_per_call``) and while the
fill sleeps, and the wall clock follows the batch's clock from the moment the batch starts.
Each refusal is a 503, as all 474 of D14's failures were.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from backend.domain.cache_fill import FillRun
from backend.domain.enums import FillTier
from backend.services.cache_fill_constants import FILL_LIMITS, FillLimits
from backend.services.cache_fill_service import (
    BatchOutcome,
    BatchPorts,
    NameTitleCall,
    run_batch,
)
from tests.fakes.mb_client import FakeMbClient

T0 = datetime(2026, 11, 24, 20, 6, tzinfo=UTC)
"""When the run started: D14's run began at 20:06."""

PAUSE = 10.0
REFUSED = "503"


@dataclass
class MusicBrainz:
    """The scripted server: each name + title search takes ``seconds_per_call`` and is answered,
    or refused with a 503 where the script says so. It keeps the time: the batch's clock and,
    from ``wall_at_start``, the wall clock."""

    seconds_per_call: float
    script: list[str | None] = field(default_factory=list)
    wall_at_start: datetime = T0
    clock: float = 0.0
    titles: list[str] = field(default_factory=list)
    sleeps: list[tuple[int, float]] = field(default_factory=list)  # (requests before, seconds)

    def now(self) -> datetime:
        return self.wall_at_start + timedelta(seconds=self.clock)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append((len(self.titles), seconds))
        self.clock += seconds

    def ask(self, title: str) -> dict[str, Any]:
        self.titles.append(title)
        self.clock += self.seconds_per_call
        answer = self.script.pop(0) if self.script else None
        if answer == REFUSED:
            request = httpx.Request("GET", "https://musicbrainz.org/ws/2/recording")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError(
                "503 Service Unavailable", request=request, response=response
            )
        return {"count": 0, "title_only": False, "recordings": []}


class Client(FakeMbClient):
    """The fill's one-attempt client, answering through the scripted server."""

    def __init__(self, server: MusicBrainz) -> None:
        super().__init__()
        self.server = server

    def search_recordings_by_name_title(
        self, name_words: Sequence[str], title: str
    ) -> dict[str, Any]:
        return self.server.ask(title)


def plan(count: int) -> list[NameTitleCall]:
    """*count* payoff searches, "Song 1" first."""
    return [
        NameTitleCall(FillTier.PAYOFF, (f"act{n}",), f"Song {n}", frozenset({f"act{n}"}))
        for n in range(1, count + 1)
    ]


def songs(count: int) -> list[str]:
    return [f"Song {n}" for n in range(1, count + 1)]


def ports(server: MusicBrainz) -> BatchPorts:
    return BatchPorts(
        client=Client(server),
        commit=lambda: None,
        clock=lambda: server.clock,
        report=lambda _row: None,
        sleep=server.sleep,
        now=server.now,
        requests=lambda: len(server.titles),
    )


def batch(
    server: MusicBrainz, calls: Sequence[NameTitleCall], limits: FillLimits = FILL_LIMITS
) -> BatchOutcome:
    """One batch of a run that started at T0."""
    return run_batch(calls, FillRun.start(T0), limits, ports(server))


def run_on(
    server: MusicBrainz, calls: Sequence[NameTitleCall], limits: FillLimits = FILL_LIMITS
) -> BatchOutcome:
    """The run from this batch on, each batch handed on starting at once (the worker is idle, so
    Huey runs it straight away) with the calls not yet asked; the run's last batch's outcome."""
    run = FillRun.start(T0)
    while True:
        left = [call for call in calls if call.title not in server.titles]
        outcome = run_batch(left, run, limits, ports(server))
        if outcome.next_run is None:
            return outcome
        run = outcome.next_run


def batch_start(run_age: timedelta, at_second: float) -> datetime:
    """The wall clock a batch starts at, so that *at_second* seconds into it the run (started at
    T0) is *run_age* old."""
    return T0 + run_age - timedelta(seconds=at_second)


# --- D18: no request follows, so the batch ends there -------------------------------------------


def test_a_refusal_in_a_batchs_last_10_s_after_the_runs_minutes_ends_the_batch() -> None:
    """D18, S9 (D14's case): the run's minutes are already up when the batch's third call is
    refused at 102 s, so the pause would end past 110 s and no batch follows: the refused call is
    the run's last call. No pause, no fourth request; the run ends ``time``, the rest waiting in
    payoff (D12). Before the fix, the batch asked "Song 4" at once."""
    server = MusicBrainz(
        seconds_per_call=34.0,
        script=[None, None, REFUSED],
        wall_at_start=batch_start(timedelta(minutes=121), at_second=102),
    )

    outcome = batch(server, plan(6))

    assert server.titles == songs(3)
    assert server.sleeps == []
    assert outcome.progress["ended"] == "time"
    assert outcome.progress["next_batch"] is False
    assert outcome.next_run is None
    assert outcome.progress["detail"] == "payoff, stopped (2 hours up)"


def test_a_refusal_whose_pause_would_spend_the_runs_minutes_ends_the_run() -> None:
    """D18, S9 (the coordinator's reading): the third call is refused 102 s into the batch and
    119:55 into the run, so its pause would end at 120:05, when the run hands on no batch. No
    request follows it: no pause, no further call in the batch, and no batch handed on to ask at
    once before 120:00. The run ends ``time``."""
    server = MusicBrainz(
        seconds_per_call=34.0,
        script=[None, None, REFUSED],
        wall_at_start=batch_start(timedelta(minutes=119, seconds=55), at_second=102),
    )

    outcome = run_on(server, plan(6))

    assert server.titles == songs(3)
    assert server.sleeps == []
    assert outcome.progress["ended"] == "time"
    assert outcome.progress["next_batch"] is False
    assert outcome.progress["detail"] == "payoff, stopped (2 hours up)"


def test_a_batchs_last_call_refused_inside_the_runs_last_pause_ends_the_run() -> None:
    """D18, S9 (the coordinator's reading): a batch of 3 calls, its last refused 119:55 into the
    run. Its pause would end at 120:05, so no request follows: no pause, and no batch handed on
    to ask at once before 120:00. The run ends ``time``."""
    server = MusicBrainz(
        seconds_per_call=1.0,
        script=[None, None, REFUSED],
        wall_at_start=batch_start(timedelta(minutes=119, seconds=55), at_second=3),
    )

    outcome = run_on(server, plan(6), replace(FILL_LIMITS, batch_calls=3))

    assert server.titles == songs(3)
    assert server.sleeps == []
    assert outcome.progress["ended"] == "time"
    assert outcome.progress["next_batch"] is False


# --- what D18 leaves as it was ------------------------------------------------------------------


def test_with_minutes_to_spare_the_same_refusal_pauses_and_then_the_batch_ends() -> None:
    """S9 (R2b-b): the same refusal at 102 s, with the run 30 minutes old. A request follows in
    the run's next batch, so the fill pauses 10 s; the pause takes the batch past 110 s, so it
    starts no further call and hands on the next batch."""
    server = MusicBrainz(
        seconds_per_call=34.0,
        script=[None, None, REFUSED],
        wall_at_start=batch_start(timedelta(minutes=30), at_second=102),
    )

    outcome = batch(server, plan(6))

    assert server.titles == songs(3)
    assert server.sleeps == [(3, PAUSE)]
    assert outcome.progress["ended"] is None
    assert outcome.progress["next_batch"] is True


def test_a_refusal_early_in_a_batch_pauses_once_the_runs_minutes_are_up() -> None:
    """S9, D11: a batch that started inside the run's 120 minutes makes its calls to 110 s, even
    once the minutes are up. Its second call is refused at 68 s, 120:38 into the run: another
    call follows in the batch, so the fill pauses 10 s and asks "Song 3" at 78 s; then the batch
    is past 110 s and the run ends ``time``."""
    server = MusicBrainz(
        seconds_per_call=34.0,
        script=[None, REFUSED, None],
        wall_at_start=batch_start(timedelta(minutes=120), at_second=30),
    )

    outcome = batch(server, plan(6))

    assert server.titles == songs(3)
    assert server.sleeps == [(2, PAUSE)]
    assert outcome.progress["ended"] == "time"
    assert outcome.progress["next_batch"] is False
