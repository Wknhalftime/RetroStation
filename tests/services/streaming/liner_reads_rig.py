"""The liner rig rewired with the D85 re-read's own connections (follow-up to PR L2, #171;
locked with ``test_liner_read_bounds.py`` and ``test_liner_walked_position.py``).

``rewired`` rebuilds a liner rig's service with two repository factories, as production
wires them (``main.py``): ``general`` for the stream's ordinary reads (the day, the station,
the settings) and ``short`` for the reads that must never hold an item, behind
``StreamPorts.cue_reread_repos`` (D85's short statement and lock limits, D86(c)). The clocks,
engine fake, gated sleep, shuffle, bookmarks and liners root are the rig's own, so the rig's
helpers (``open``, ``hand_out``, ``started``, ``elapse``, ``label``) work unchanged.

- ``HeldLiners`` is the liner fake whose ``playable`` read can be held inside the fake (a
  slow database), counting every read that enters it.
- ``AnalysedSince`` answers the D85 re-read with cues stored since tune-in, and otherwise
  with what the rig's day carries.

Project rules: fakes implement the repository ABCs; no sleep-based ordering (a held read is
released by a ``threading.Event``).
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from backend.domain.liners import PlayableLiner
from backend.domain.streaming import CuePoints, ScheduleItem
from backend.repositories.playable_schedule import PlayableScheduleRepository
from backend.services.streaming.cue_reread import CueRereadLimits
from backend.services.streaming.service import (
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.services.streaming.liner_rig import LinerReads, LinerRig
from tests.services.streaming.schedule import BACKEND

__all__ = ["AnalysedSince", "HeldLiners", "held_liners", "rewired"]

_STEADY_EPOCH = datetime(2000, 1, 1)


class HeldLiners(LinerReads):
    """The rig's liner fake; a ``playable`` read waits inside the fake while ``hold`` is set
    and not yet released. ``entered`` counts the reads that reached the fake, ``inside`` is
    set once one has, and ``left`` once one has returned."""

    def __init__(self, cues: FakeStreamCueRepository) -> None:
        super().__init__(cues)
        self.hold: threading.Event | None = None
        self.entered = 0
        self.inside = threading.Event()
        self.left = threading.Event()

    def playable(self, station_id: UUID) -> list[PlayableLiner]:
        self.entered += 1
        self.inside.set()
        if self.hold is not None:
            self.hold.wait(5)
        answer = super().playable(station_id)
        self.left.set()
        return answer


class AnalysedSince(PlayableScheduleRepository):
    """The D85 re-read's schedule reader: ``stored`` holds cues stored since tune-in; any
    other file answers what ``day_reader`` (the rig's schedule) carries. Days are read
    through ``day_reader`` too, though the service reads them on the general connection."""

    def __init__(
        self, day_reader: PlayableScheduleRepository, stored: dict[UUID, CuePoints]
    ) -> None:
        self._day_reader = day_reader
        self.stored = stored

    def get_day(self, station_id: UUID, day: date) -> list[ScheduleItem]:
        return self._day_reader.get_day(station_id, day)

    def file_cues(self, file_id: UUID) -> CuePoints | None:
        if file_id in self.stored:
            return self.stored[file_id]
        return self._day_reader.file_cues(file_id)


def held_liners(rig: LinerRig) -> HeldLiners:
    """A ``HeldLiners`` holding the rig's liners (``a`` and ``b``), with their points."""
    held = HeldLiners(rig.cues)
    for liner in rig.liners.liners.values():
        held.add(liner)
    return held


def rewired(
    rig: LinerRig,
    tmp_path: Path,
    *,
    general: StreamRepos,
    short: StreamRepos,
    limits: CueRereadLimits | None = None,
) -> LinerRig:
    """``rig`` with a new service reading ``general`` through ``StreamPorts.repos`` and
    ``short`` through ``StreamPorts.cue_reread_repos``, its re-read limits ``limits`` (the
    D86(c) defaults when ``None``). ``short.liners``, when a ``HeldLiners``, becomes the rig's
    ``liners``."""

    def open_general() -> AbstractContextManager[StreamRepos]:
        return nullcontext(general)

    def open_short() -> AbstractContextManager[StreamRepos]:
        return nullcontext(short)

    steady = rig.steady

    def steady_seconds() -> float:
        return (steady.now - _STEADY_EPOCH) / timedelta(seconds=1)

    ports = StreamPorts(
        repos=open_general,
        start_engine=rig.engines,
        clock=rig.clock,
        steady=steady_seconds,
        cue_reread_repos=open_short,
        sleep=rig.sleep,
        shuffle=rig.shuffle,
    )
    config = StreamServiceConfig(
        callback_base_url=BACKEND,
        log_dir=tmp_path / "logs",
        liners_dir=rig.liners_dir,
        cue_reread=CueRereadLimits() if limits is None else limits,
    )
    service = StreamService(ports, rig.bookmarks, config)
    if isinstance(short.liners, HeldLiners):
        return replace(rig, service=service, liners=short.liners)
    return replace(rig, service=service)
