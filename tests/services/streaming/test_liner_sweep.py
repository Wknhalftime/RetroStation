"""Station liners, PR L2 Task 12a (carried from the L1 review, T4-a and T4-b): liner files the
engine held open, and crash leftovers, are swept from the station's folder.

Once liners play, a Liquidsoap session can hold a liner file open, so a delete's file removal
(and a station folder's removal) fails routinely on Windows. The rules (Task 12a as ruled after
the L2 test audit, I5):

- ``delete_liner`` removes the row, commits, then removes its own file at once (one attempt, a
  failure logged), then sweeps.
- ``sweep_station_liners(ports, station_id)`` removes, best effort, in the station's folder
  only: files named like a liner (``LINER_FILE_NAME``) whose id has no row, and ``.partial``
  files, each only when older than ``PARTIAL_MAX_AGE`` (the sign-off's hour). A fresh row-less
  liner file may be a concurrent upload's, installed but not yet committed, so it is left
  alone. Nothing else is touched. Every failure is logged as ``stream_liner_cleanup_failed``
  and never raised. ``add_liner`` and ``delete_liner`` run it after their commit.
- ``discard_station_liners`` removes the folder file by file, logging each failure and
  carrying on.

Requirements: LR3 (delete removes the row, the file and its edit; deleting a station deletes
its liners and their folder). Fakes plus ``tmp_path``; ages are file mtimes set with
``os.utime``. A file "held open" is either a directory in its place (removing it fails on every
platform, as in L1's locked tests) or, for the folder removal, a file whose unlink the OS
refuses.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from backend.domain.broadcast import BroadcastStation
from backend.domain.library import AudioHash
from backend.domain.liners import StationLiner
from backend.domain.streaming import ClipFormat, ProbedClip
from backend.services.streaming.liners import (
    LinerPorts,
    add_liner,
    delete_liner,
    discard_station_liners,
    liner_folder,
    station_liner_folder,
    sweep_station_liners,
)
from backend.services.streaming.sign_off import PARTIAL_MAX_AGE, ClipUpload
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.station_liners import FakeStationLiners

CLEANUP_FAILED = "stream_liner_cleanup_failed"
STATION_ID = UUID("0a1b2c3d-4e5f-4a6b-9c7d-8e9fa0b1c2d3")
OTHER_STATION_ID = UUID("1b2c3d4e-5f60-4b7c-8d9e-0fa1b2c3d4e5")
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def wav_bytes(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8_000)
        out.writeframes(b"\x10\x00" * round(seconds * 8_000))
    return buffer.getvalue()


@dataclass
class Station:
    """One station's liners: the fakes, its folder under ``work``, and recording ports."""

    work: Path
    liners: FakeStationLiners = field(default_factory=FakeStationLiners)
    stations: FakeBroadcastStationRepository = field(default_factory=FakeBroadcastStationRepository)
    ids: Iterator[UUID] = field(default_factory=lambda: iter([uuid4() for _ in range(4)]))

    def __post_init__(self) -> None:
        self.stations.create(BroadcastStation(STATION_ID, "WKRP"))
        self.stations.create(BroadcastStation(OTHER_STATION_ID, "WKRQ"))

    @property
    def ports(self) -> LinerPorts:
        return LinerPorts(
            liners=self.liners,
            stations=self.stations,
            folder=liner_folder(self.work),
            probe=lambda path: ProbedClip(ClipFormat.WAV, 4_000),
            commit=lambda: None,
            clock=lambda: datetime.now(UTC),  # real time: partial ages are file mtimes
            new_id=lambda: next(self.ids),
        )

    @property
    def folder(self) -> Path:
        return station_liner_folder(self.work, STATION_ID)

    def seed(self, station_id: UUID = STATION_ID, liner_id: UUID | None = None) -> Path:
        """A stored WAV liner: its row in the fake and its file on disk."""
        liner_id = liner_id if liner_id is not None else uuid4()
        data = b"RIFF" + liner_id.bytes
        liner = StationLiner(
            id=liner_id,
            station_id=station_id,
            name="Sweeper",
            file_name=f"{liner_id}.wav",
            format=ClipFormat.WAV,
            audio_hash=AudioHash.parse("audio-sha256:" + hashlib.sha256(data).hexdigest()),
            duration_ms=4_000,
            markers=None,
            uploaded_at=NOW,
        )
        self.liners.add(liner)
        folder = station_liner_folder(self.work, station_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / liner.file_name
        path.write_bytes(data)
        return path

    def orphan(self, suffix: str = "flac") -> Path:
        """A file named like a liner whose id has no row (a delete's file left behind, or a
        crash between install and commit)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{uuid4()}.{suffix}"
        path.write_bytes(b"left behind")
        return path


@pytest.fixture
def station(tmp_path: Path) -> Station:
    return Station(tmp_path / "work")


def cleanup_failures(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == CLEANUP_FAILED]


def age(path: Path) -> Path:
    """Make ``path`` older than ``PARTIAL_MAX_AGE`` (two hours old)."""
    old = time.time() - 2 * PARTIAL_MAX_AGE.total_seconds()
    os.utime(path, (old, old))
    return path


def test_sweep_removes_orphan_liner_files(station: Station) -> None:
    """LR3: an old liner-named file with no row goes, in any of the three formats. A liner's
    own file stays; a fresh row-less file (an upload between install and commit) stays; and
    another station's folder is not touched, not even its old orphans."""
    kept = age(station.seed())
    elsewhere = age(station.seed(OTHER_STATION_ID))
    their_orphan = elsewhere.parent / f"{uuid4()}.flac"
    their_orphan.write_bytes(b"left behind")
    age(their_orphan)
    orphans = [age(station.orphan(fmt)) for fmt in ("flac", "mp3", "wav")]
    in_flight = station.orphan()

    sweep_station_liners(station.ports, STATION_ID)

    assert kept.exists()
    assert elsewhere.exists()
    assert their_orphan.exists()
    assert in_flight.exists(), "an upload between install and commit lost its file"
    assert [path for path in orphans if path.exists()] == []


def test_sweep_keeps_fresh_partials_and_drops_stale(station: Station) -> None:
    """A partial from an upload still running is kept; one older than ``PARTIAL_MAX_AGE``
    (an hour) belongs to no running upload and goes."""
    station.folder.mkdir(parents=True)
    fresh = station.folder / f"{uuid4()}.partial"
    stale = station.folder / f"{uuid4()}.partial"
    for path in (fresh, stale):
        path.write_bytes(b"staged")
    age(stale)

    sweep_station_liners(station.ports, STATION_ID)

    assert fresh.exists()
    assert not stale.exists()


def test_sweep_leaves_unrelated_files(station: Station) -> None:
    """Nothing but orphaned liner files and partials is touched, however old: not a file whose
    name is not a liner's (an ``.ogg`` with a liner-like id included). A station with no
    folder is not an error."""
    station.folder.mkdir(parents=True)
    names = ("notes.txt", "a.flac", "cover.jpg", f"{uuid4()}.ogg")
    unrelated = [station.folder / name for name in names]
    for path in unrelated:
        path.write_bytes(b"mine")
        age(path)

    with capture_logs() as logs:
        sweep_station_liners(station.ports, STATION_ID)
        sweep_station_liners(station.ports, uuid4())

    assert [path for path in unrelated if not path.exists()] == []
    assert cleanup_failures(logs) == []


def test_sweep_failure_is_logged_not_raised(station: Station) -> None:
    """An old orphan that cannot be removed (a directory named like a liner file) is logged as
    ``stream_liner_cleanup_failed``, once, and the sweep carries on with the rest."""
    station.folder.mkdir(parents=True)
    stuck = station.folder / f"{uuid4()}.flac"
    stuck.mkdir()
    age(stuck)
    orphan = age(station.orphan())

    with capture_logs() as logs:
        sweep_station_liners(station.ports, STATION_ID)

    assert len(cleanup_failures(logs)) == 1
    assert stuck.exists()
    assert not orphan.exists()


def test_delete_removes_its_own_file_at_once(station: Station) -> None:
    """LR3: a delete removes its own liner's file straight away, however new it is; a fresh
    row-less file in the folder (another upload, not yet committed) is left alone."""
    deleted = station.seed()
    in_flight = station.orphan()

    with capture_logs() as logs:
        delete_liner(station.ports, STATION_ID, UUID(deleted.stem))

    assert not deleted.exists()
    assert in_flight.exists()
    assert cleanup_failures(logs) == []


def release(held: Path) -> None:
    """The engine lets go of the file: what is left is an ordinary file at the same path, with
    the liner's upload-time age (here older than ``PARTIAL_MAX_AGE``)."""
    held.rmdir()
    held.write_bytes(b"left behind")
    age(held)


NextCall = Callable[[Station], None]


def add_another(station: Station) -> None:
    add_liner(station.ports, STATION_ID, ClipUpload(name="ID.wav", data=wav_bytes(0.5)))


def delete_another(station: Station) -> None:
    other = station.seed()
    delete_liner(station.ports, STATION_ID, UUID(other.stem))


@pytest.mark.parametrize(
    "next_call", [add_another, delete_another], ids=["next add", "next delete"]
)
def test_delete_then_sweep_retries_a_file_left_behind(
    station: Station, next_call: NextCall
) -> None:
    """LR3 (T4-a): a delete whose file the engine holds open logs its one failed attempt and
    leaves the file (the sweep that follows leaves a fresh file alone); once released, the
    station's next add or delete removes it."""
    held = station.seed()
    held.unlink()
    held.mkdir()  # held open: removing it fails on every platform

    with capture_logs() as logs:
        delete_liner(station.ports, STATION_ID, UUID(held.stem))

    assert len(cleanup_failures(logs)) == 1
    assert held.exists()

    release(held)
    next_call(station)

    assert not held.exists()


def test_discard_station_liners_carries_on_past_a_failure(
    station: Station, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LR3 (T4-b): deleting a station removes its folder file by file. The first file the
    discard reaches is held open (the OS refuses to remove it): that failure is logged, once,
    and the file stays; every other file is gone."""
    files = [station.seed() for _ in range(3)] + [station.orphan()]
    held_open: list[str] = []

    def guarded(real: Callable[..., None]) -> Callable[..., None]:
        def unlink(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
            name = os.path.basename(os.fspath(path))
            if not held_open:
                held_open.append(name)  # the first file the discard reaches is held open
            if name == held_open[0]:
                raise PermissionError(13, "The process cannot access the file", os.fspath(path))
            real(path, dir_fd=dir_fd)

        return unlink

    monkeypatch.setattr(os, "unlink", guarded(os.unlink))
    monkeypatch.setattr(os, "remove", guarded(os.remove))

    with capture_logs() as logs:
        discard_station_liners(liner_folder(station.work), STATION_ID)

    monkeypatch.undo()
    assert [path.name for path in files if path.exists()] == held_open
    failures = cleanup_failures(logs)
    assert all(e["event"] == CLEANUP_FAILED for e in logs if e["log_level"] in ("warning", "error"))
    naming_held = [e for e in failures if any(held_open[0] in str(v) for v in e.values())]
    assert len(naming_held) == 1
