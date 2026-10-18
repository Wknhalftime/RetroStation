"""The liner rig (PR L2, Task 11; locked with ``test_liner_playout.py``): the events rig's
stream service (``events_rig.py``: a wall clock, a separate steady clock and a gated sleep,
D47) for a station with a liner cadence and two liners, ``a`` and ``b``.

- The liners are read through a ``FakeStationLiners`` whose ``playable`` read is counted and
  can be made to fail once (D88: ``StreamReadError``), as the sign-off rig's settings read can.
- The service's ``shuffle`` port is a ``RecordedShuffle``: it records a copy of each list it is
  given, then applies the move planned for that call (by default a descending sort, so the
  order never depends on the order the reader lists the liners in).
- The service's ``liners_dir`` is ``tmp_path / "liners"``, the liners root; a liner is sent
  from ``<liners_dir>/<station_id>/<file_name>`` (plan note 4).
- The day is ``count`` songs, ``s0``..``s<n>``, 200 s each, logged 200 s apart from 06:00, no
  cues. NOW is 06:01, so a clock tune-in lands 60 s into ``s0``.

``b``'s id sorts after ``a``'s, so with the default shuffle a session's first liner is ``b``,
then ``a``, whatever order the reader lists them in.

Spec: "On air", "Cadence", "Which liner", "Timeless". Project rules: fakes implement the
repository ABCs; no sleep-based ordering (``GatedSleep``, explicit clocks).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from backend.domain.broadcast import BroadcastStation
from backend.domain.library import AudioHash
from backend.domain.liners import LinerCadence, PlayableLiner, StationLiner
from backend.domain.streaming import ClipFormat, ScheduleItem
from backend.playout.liquidsoap_process import long_path
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.payload import FinalClip, ItemPayload
from backend.services.streaming.service import (
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.station_liners import FakeStationLiners
from tests.fakes.stream_cues import FakeStreamCueRepository
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
    "A_MS",
    "B_MS",
    "LINER_A",
    "LINER_B",
    "SONG_S",
    "ascending",
    "descending",
    "LinerReads",
    "LinerRig",
    "RecordedShuffle",
    "make_liner_rig",
    "songs",
]

LINER_A = UUID("aaaaaaaa-0000-4000-8000-00000000000a")
LINER_B = UUID("bbbbbbbb-0000-4000-8000-00000000000b")
A_MS = 4_000
B_MS = 7_000
SONG_S = 200
_UPLOADED = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
_STEADY_EPOCH = datetime(2000, 1, 1)

type Move = Callable[[list[UUID]], None]


def descending(ids: list[UUID]) -> None:
    """A shuffle that sorts the ids in descending order."""
    ids.sort(reverse=True)


def ascending(ids: list[UUID]) -> None:
    """A shuffle that sorts the ids in ascending order."""
    ids.sort()


def station_liner(
    liner_id: UUID, name: str, duration_ms: int, uploaded_at: datetime
) -> StationLiner:
    """A stored WAV liner of ``STATION`` with no edit; its hash is the sha256 of its id."""
    return StationLiner(
        id=liner_id,
        station_id=STATION,
        name=name,
        file_name=f"{liner_id}.wav",
        format=ClipFormat.WAV,
        audio_hash=AudioHash.parse("audio-sha256:" + hashlib.sha256(liner_id.bytes).hexdigest()),
        duration_ms=duration_ms,
        markers=None,
        uploaded_at=uploaded_at,
    )


class LinerReads(FakeStationLiners):
    """The liner fake; each ``playable`` read is counted, and fails once when told to."""

    def __init__(self, cues: FakeStreamCueRepository) -> None:
        super().__init__(cues)
        self.reads = 0
        self._fail_next: Exception | None = None

    def fail_next_read(self, error: Exception) -> None:
        self._fail_next = error

    def playable(self, station_id: UUID) -> list[PlayableLiner]:
        self.reads += 1
        if self._fail_next is not None:
            error, self._fail_next = self._fail_next, None
            raise error
        return super().playable(station_id)


@dataclass
class RecordedShuffle:
    """The injected shuffle: records a copy of each list it is given, then applies
    ``moves[n]`` on its n-th call (the last move once they run out)."""

    moves: list[Move] = field(default_factory=lambda: [descending])
    given: list[list[UUID]] = field(default_factory=list)

    def __call__(self, ids: list[UUID]) -> None:
        self.given.append(list(ids))
        self.moves[min(len(self.given), len(self.moves)) - 1](ids)


def songs(count: int) -> list[ScheduleItem]:
    """``count`` songs, ``s0`` at 06:00 then one every 200 s, titled "Song n" by "Artist n"."""
    start = datetime.combine(DAY, datetime.min.time()) + timedelta(hours=6)
    return [
        song(
            (start + timedelta(seconds=SONG_S * n)).time().isoformat(),
            SONG_S,
            title=f"Song {n}",
            artist=f"Artist {n}",
        )
        for n in range(count)
    ]


@dataclass
class LinerRig(EventsRig):
    """``EventsRig`` with the station's liners, the cue fake their points are read from, the
    recorded shuffle and the liners root."""

    liners: LinerReads = field(kw_only=True)
    cues: FakeStreamCueRepository = field(kw_only=True)
    shuffle: RecordedShuffle = field(kw_only=True)
    liners_dir: Path = field(kw_only=True)
    day: list[ScheduleItem] = field(kw_only=True, default_factory=list)

    def set_day(self, items: list[ScheduleItem]) -> list[ScheduleItem]:
        """The station's day; songs are labelled by their position in it (``label``)."""
        self.day = list(items)
        self.schedule.set_day(STATION, DAY, items)
        return items

    def set_cadence(self, songs_between: int | None) -> None:
        """Change the station's cadence, as the Liners tab does."""
        self.stations.set_liner_cadence(STATION, LinerCadence.from_setting(songs_between))

    def liner_file(self, liner_id: UUID) -> Path:
        """Where the liner's file is stored: ``<liners_dir>/<station_id>/<file_name>``."""
        return self.liners_dir / str(STATION) / self.liners.liners[liner_id].file_name

    def label(self, payload: ItemPayload) -> str:
        """``"a"`` or ``"b"`` for a liner, ``"s<n>"`` for the day's song n."""
        for name, liner_id in (("a", LINER_A), ("b", LINER_B)):
            if payload.path == long_path(self.liners_dir / str(STATION) / f"{liner_id}.wav"):
                return name
        for n, item in enumerate(self.day):
            if item.file is not None and payload.path == long_path(Path(item.file.path)):
                return f"s{n}"
        raise AssertionError(f"sent an unknown path: {payload.path}")

    async def hand_out(self, sid: str, count: int, *, first: int = 0) -> list[str]:
        """The engine asks for seqs ``first``..``first + count - 1`` in turn (prefetch);
        each one's label."""
        return [self.label(await self.item(sid, seq)) for seq in range(first, first + count)]


def make_liner_rig(
    tmp_path: Path,
    *,
    cadence: int | None = 2,
    with_liners: bool = True,
    final_clip: FinalClip | None = None,
    count: int = 10,
) -> LinerRig:
    """The service for ``STATION`` with ``cadence`` (None: blank), liners ``a`` (4 s) and
    ``b`` (7 s) unless ``with_liners`` is false (an empty library), ``final_clip`` as the
    default sign-off, and a day of ``count`` songs."""
    stations = FakeBroadcastStationRepository()
    stations.create(
        BroadcastStation(
            id=STATION, call_letters=CALL, liner_cadence=LinerCadence.from_setting(cadence)
        )
    )
    settings = FakeUserSettingRepository()
    schedule = CountingSchedule()
    cues = FakeStreamCueRepository()
    liners = LinerReads(cues)
    if with_liners:
        liners.add(station_liner(LINER_A, "a sweeper", A_MS, _UPLOADED))
        liners.add(station_liner(LINER_B, "b legal ID", B_MS, _UPLOADED + timedelta(minutes=1)))
    repos = StreamRepos(stations=stations, settings=settings, schedule=schedule, liners=liners)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    clock, steady = Clock(), Clock(STEADY_START)
    sleep = GatedSleep(steady, clock)
    engines = RaisingEngineStarts()
    shuffle = RecordedShuffle()

    def steady_seconds() -> float:
        return (steady.now - _STEADY_EPOCH) / timedelta(seconds=1)

    ports = StreamPorts(
        repos=open_repos,
        start_engine=engines,
        clock=clock,
        steady=steady_seconds,
        sleep=sleep,
        shuffle=shuffle,
    )
    liners_dir = tmp_path / "liners"
    config = StreamServiceConfig(
        callback_base_url=BACKEND,
        log_dir=tmp_path / "logs",
        final_clip=final_clip,
        liners_dir=liners_dir,
    )
    bookmarks = BookmarkStore()
    service = StreamService(ports, bookmarks, config)
    rig = LinerRig(
        service,
        stations,
        schedule,
        settings,
        bookmarks,
        engines,
        clock,
        steady=steady,
        sleep=sleep,
        liners=liners,
        cues=cues,
        shuffle=shuffle,
        liners_dir=liners_dir,
    )
    rig.set_day(songs(count))
    return rig
