"""The stream service rig with a real CueReporter in front of a recording cue owner, and a
schedule whose stored cues a test changes mid-session (spec: D78, D79, D85). The same fakes
as the locked D2 rig, built the same way, plus what the cue owner was asked."""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import CuePoints
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.cue_reports import CueReporter
from backend.services.streaming.cue_reread import CueRereadLimits
from backend.services.streaming.service import (
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.services.streaming.helpers import (
    BACKEND,
    CALL,
    STATION,
    Clock,
    CountingSchedule,
    EngineStarts,
    Rig,
)


@dataclass
class CueOwner:
    """A fake cue owner (the request a CueReporter hands reports to): records each file it
    is asked about; with ``fail`` every request raises, as a locked queue file would; with
    ``hold`` set, each request waits until the test sets it (a busy queue file)."""

    asked: list[UUID] = field(default_factory=list)
    fail: bool = False
    hold: threading.Event | None = None

    def __call__(self, file_id: UUID) -> None:
        if self.hold is not None:
            assert self.hold.wait(timeout=10), "the test never released the owner"
        if self.fail:
            raise OSError("database is locked")
        self.asked.append(file_id)


class StoredCuesSchedule(CountingSchedule):
    """The schedule fake as the cue store sees it now (D85).

    ``stored`` overrides what ``file_cues`` answers for a file (a row stored, or None for a
    row gone, since tune-in); ``fail`` makes every re-read raise; ``hold``, when set, makes a
    re-read wait until the test sets it. ``cue_reads`` records each file re-read;
    ``entered`` is set once a re-read has begun and ``left`` once it has ended, however it
    ended, so a test can prove an item came back while its re-read was still running.
    """

    def __init__(self) -> None:
        super().__init__()
        self.stored: dict[UUID, CuePoints | None] = {}
        self.fail: OSError | None = None
        self.hold: threading.Event | None = None
        self.entered = threading.Event()
        self.left = threading.Event()
        self.cue_reads: list[UUID] = []

    def file_cues(self, file_id: UUID) -> CuePoints | None:
        self.cue_reads.append(file_id)
        self.entered.set()
        try:
            if self.hold is not None:
                assert self.hold.wait(timeout=10), "the test never released the re-read"
            if self.fail is not None:
                raise self.fail
            if file_id in self.stored:
                return self.stored[file_id]
            return super().file_cues(file_id)
        finally:
            self.left.set()


RIG_REREAD = CueRereadLimits(timeout_s=5.0)


@dataclass
class ReportingRig:
    rig: Rig
    owner: CueOwner
    reporter: CueReporter
    schedule: StoredCuesSchedule


def make_reporting_rig(
    tmp_path: Path,
    *,
    memory: int = 4096,
    owner: CueOwner | None = None,
    reread: CueRereadLimits | None = None,
) -> ReportingRig:
    """The rig. Its re-read limit defaults to 5 s, not the production 0.5 s, so the fakes'
    instant reads never race it under load (audit N4); RR-6 pins the production default."""
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    user_settings = FakeUserSettingRepository(None)
    schedule = StoredCuesSchedule()
    repos = StreamRepos(stations=stations, settings=user_settings, schedule=schedule)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    clock = Clock()
    engines = EngineStarts()
    bookmarks = BookmarkStore()
    cue_owner = owner if owner is not None else CueOwner()
    reporter = CueReporter(cue_owner)
    ports = StreamPorts(repos=open_repos, start_engine=engines, clock=clock, cue_reports=reporter)
    config = StreamServiceConfig(
        callback_base_url=BACKEND,
        log_dir=tmp_path / "logs",
        no_cue_memory=memory,
        cue_reread=reread if reread is not None else RIG_REREAD,
    )
    service = StreamService(ports, bookmarks, config)
    rig = Rig(service, stations, schedule, user_settings, bookmarks, engines, clock)
    return ReportingRig(rig, cue_owner, reporter, schedule)
