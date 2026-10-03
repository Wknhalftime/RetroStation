"""The sign-off rig (PR G1, Task 4; locked with ``test_sign_off_playout.py``): the D2 stream
service rig with the events rig's steady clock and gated sleep (``events_rig.py``), wired
to a sign-off folder (``StreamServiceConfig.sign_off_dir``) and a settings fake whose
sign-off read can be made to fail once (D88) and counts its reads (PG4: the clip is read
when the schedule ends, never at tune-in).

Spec: D26 (the user's own sign-off clip); D78a ("ended" means signed off); D88 (a failed
read answers the engine with an error, and the engine retries). Project rules: fakes
implement the repository ABCs; no sleep-based ordering (``GatedSleep``, explicit clocks).
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import EndOfScheduleError, ScheduleItem, SignOff
from backend.domain.system import UserSetting
from backend.playout.liquidsoap_process import long_path
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.payload import FinalClip
from backend.services.streaming.service import (
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.services.streaming.events_rig import (
    STEADY_START,
    EventsRig,
    GatedSleep,
    RaisingEngineStarts,
)
from tests.services.streaming.helpers import Clock, CountingSchedule
from tests.services.streaming.schedule import BACKEND, CALL, DAY, STATION, song

__all__ = [
    "SIGN_OFF_KEY",
    "SignOffRig",
    "SignOffSettings",
    "make_sign_off_rig",
    "morning",
]

SIGN_OFF_KEY = "stream_sign_off"
_STEADY_EPOCH = datetime(2000, 1, 1)
_SONG_S = 200


class SignOffSettings(FakeUserSettingRepository):
    """The settings fake; a read of the sign-off is counted, and fails once when told to."""

    def __init__(self) -> None:
        super().__init__()
        self.sign_off_reads = 0
        self._fail_next: Exception | None = None

    def fail_next_read(self, error: Exception) -> None:
        self._fail_next = error

    def get(self, key: str) -> UserSetting | None:
        if key == SIGN_OFF_KEY:
            self.sign_off_reads += 1
            if self._fail_next is not None:
                error, self._fail_next = self._fail_next, None
                raise error
        return super().get(key)


@dataclass
class SignOffRig(EventsRig):
    """``EventsRig`` with a sign-off folder and the failing settings fake."""

    folder: Path = field(kw_only=True)
    sign_off: SignOffSettings = field(kw_only=True)

    def set_clip(self, clip: SignOff) -> None:
        """Store the clip's setting, as its upload does (the file itself is not needed)."""
        self.sign_off.upsert(UserSetting(key=SIGN_OFF_KEY, value=clip.to_setting()))

    def store_raw(self, value: str) -> None:
        """Store any value under the sign-off key (a bad one, for instance)."""
        self.sign_off.upsert(UserSetting(key=SIGN_OFF_KEY, value=value))

    def remove_clip(self) -> None:
        self.sign_off.delete(SIGN_OFF_KEY)

    def fail_next_read(self, error: Exception) -> None:
        self.sign_off.fail_next_read(error)

    def clip_path(self, clip: SignOff) -> str:
        """The path the service must send for ``clip``."""
        return long_path(self.folder / clip.file_name)

    async def play_to_end(self, sid: str) -> int:
        """Ask for each item in turn, starting each song and letting it play out; stop at
        the final clip (assigned, not started) or at the end. Returns that seq."""
        seq = 0
        while True:
            try:
                payload = await self.item(sid, seq)
            except EndOfScheduleError:
                return seq
            if payload.annotations.get("final") == "true":
                return seq
            self.started(sid, seq)
            self.elapse(_SONG_S)
            seq += 1


def morning(rig: SignOffRig) -> list[ScheduleItem]:
    """06:00 (200 s), 06:03:20, an unresolved play, then 06:07: seqs 0, 1 and 2 are the
    three songs, and the day after has no log, so the schedule ends at seq 3 (D39)."""
    items = [
        song("06:00:00", title="Fernando", artist="ABBA"),
        song("06:03:20", title="Mandy", artist="Barry Manilow"),
        song("06:06:40", None),
        song("06:07:00", title="Vincent", artist="Don McLean"),
    ]
    rig.schedule.set_day(STATION, DAY, items)
    return items


def make_sign_off_rig(
    tmp_path: Path, *, clip: SignOff | None = None, default_clip: FinalClip | None = None
) -> SignOffRig:
    """The service with ``sign_off_dir = tmp_path / "sign-off"`` and ``final_clip =
    default_clip``; ``clip`` is stored as the user's sign-off before anything opens."""
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    settings = SignOffSettings()
    schedule = CountingSchedule()
    repos = StreamRepos(stations=stations, settings=settings, schedule=schedule)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    clock, steady = Clock(), Clock(STEADY_START)
    sleep = GatedSleep(steady, clock)
    engines = RaisingEngineStarts()

    def steady_seconds() -> float:
        return (steady.now - _STEADY_EPOCH) / timedelta(seconds=1)

    ports = StreamPorts(
        repos=open_repos, start_engine=engines, clock=clock, steady=steady_seconds, sleep=sleep
    )
    folder = tmp_path / "sign-off"
    config = StreamServiceConfig(
        callback_base_url=BACKEND,
        log_dir=tmp_path / "logs",
        final_clip=default_clip,
        sign_off_dir=folder,
    )
    bookmarks = BookmarkStore()
    service = StreamService(ports, bookmarks, config)
    rig = SignOffRig(
        service,
        stations,
        schedule,
        settings,
        bookmarks,
        engines,
        clock,
        steady=steady,
        sleep=sleep,
        folder=folder,
        sign_off=settings,
    )
    if clip is not None:
        rig.set_clip(clip)
    return rig
