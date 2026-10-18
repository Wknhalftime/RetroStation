"""Station liners, PR L2 Task 12a fix round 1: the liner sweep's database read.

The sweep runs after an add's or a delete's commit, so it must never turn a stored change into
an error. It reads the station's stored file names once per sweep, and only when the folder
holds a liner-named file older than ``PARTIAL_MAX_AGE``; when that read loses its connection
(``StorageUnavailableError``) it logs ``stream_liner_cleanup_failed`` once and removes no liner
file, since a file whose row is unknown may be a stored liner's.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from backend.domain.broadcast import BroadcastStation, BroadcastStorageError
from backend.domain.library import AudioHash
from backend.domain.liners import LinerEntry, StationLiner
from backend.domain.streaming import ClipFormat, ProbedClip
from backend.services.streaming.liners import (
    LinerPorts,
    UploadOutcome,
    add_liner,
    delete_liner,
    liner_folder,
    station_liner_folder,
    sweep_station_liners,
)
from backend.services.streaming.sign_off import PARTIAL_MAX_AGE, ClipUpload
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.station_liners import FakeStationLiners

CLEANUP_FAILED = "stream_liner_cleanup_failed"
STATION_ID = UUID("2c3d4e5f-6071-4c8d-9e0f-1a2b3c4d5e6f")
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class ReadCountingLiners(FakeStationLiners):
    """The fake, counting the sweep's stored-name reads; ``lost`` makes each read fail as the
    PostgreSQL adapter does when its connection is gone."""

    def __init__(self, *, lost: bool = False) -> None:
        super().__init__()
        self.reads = 0
        self.lost = lost

    def list_for_station(self, station_id: UUID) -> list[LinerEntry]:
        self.reads += 1
        if self.lost:
            raise BroadcastStorageError("the database connection was lost")
        return super().list_for_station(station_id)


@dataclass
class Station:
    work: Path
    liners: ReadCountingLiners = field(default_factory=ReadCountingLiners)
    stations: FakeBroadcastStationRepository = field(default_factory=FakeBroadcastStationRepository)

    def __post_init__(self) -> None:
        self.stations.create(BroadcastStation(STATION_ID, "WKRP"))

    @property
    def ports(self) -> LinerPorts:
        return LinerPorts(
            liners=self.liners,
            stations=self.stations,
            folder=liner_folder(self.work),
            probe=lambda path: ProbedClip(ClipFormat.WAV, 4_000),
            commit=lambda: None,
            clock=lambda: NOW,
        )

    @property
    def folder(self) -> Path:
        return station_liner_folder(self.work, STATION_ID)

    def seed(self) -> Path:
        """A stored WAV liner, its file older than ``PARTIAL_MAX_AGE``."""
        liner_id = uuid4()
        data = b"RIFF" + liner_id.bytes
        self.liners.add(
            StationLiner(
                id=liner_id,
                station_id=STATION_ID,
                name="Sweeper",
                file_name=f"{liner_id}.wav",
                format=ClipFormat.WAV,
                audio_hash=AudioHash.parse("audio-sha256:" + hashlib.sha256(data).hexdigest()),
                duration_ms=4_000,
                markers=None,
                uploaded_at=NOW,
            )
        )
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{liner_id}.wav"
        path.write_bytes(data)
        return age(path)

    def orphan(self) -> Path:
        """An old liner-named file with no row."""
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{uuid4()}.flac"
        path.write_bytes(b"left behind")
        return age(path)


def age(path: Path) -> Path:
    old = time.time() - 2 * PARTIAL_MAX_AGE.total_seconds()
    os.utime(path, (old, old))
    return path


def wav_bytes(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8_000)
        out.writeframes(b"\x10\x00" * round(seconds * 8_000))
    return buffer.getvalue()


def cleanup_failures(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == CLEANUP_FAILED]


@pytest.fixture
def station(tmp_path: Path) -> Station:
    return Station(tmp_path / "work")


def test_sweep_reads_stored_names_once(station: Station) -> None:
    """Many old stored liners and orphans: one read, the orphans go, the liners stay."""
    stored = [station.seed() for _ in range(4)]
    orphans = [station.orphan() for _ in range(3)]

    sweep_station_liners(station.ports, STATION_ID)

    assert station.liners.reads == 1
    assert [path for path in stored if not path.exists()] == []
    assert [path for path in orphans if path.exists()] == []


def test_sweep_skips_the_read_without_an_old_liner_file(station: Station) -> None:
    """Only fresh liner files, an old partial and an old unrelated file: no read at all, and
    the stale partial still goes."""
    station.folder.mkdir(parents=True)
    fresh = station.folder / f"{uuid4()}.wav"
    fresh.write_bytes(b"in flight")
    partial = station.folder / f"{uuid4()}.partial"
    partial.write_bytes(b"staged")
    age(partial)
    notes = station.folder / "notes.txt"
    notes.write_bytes(b"mine")
    age(notes)

    sweep_station_liners(station.ports, STATION_ID)
    sweep_station_liners(station.ports, uuid4())  # no folder

    assert station.liners.reads == 0
    assert fresh.exists()
    assert notes.exists()
    assert not partial.exists()


def test_a_lost_read_keeps_every_liner_file(station: Station) -> None:
    """The stored-name read loses its connection: logged once, nothing raised, no liner file
    removed (stored or orphan)."""
    stored = station.seed()
    orphans = [station.orphan() for _ in range(2)]
    station.liners.lost = True

    with capture_logs() as logs:
        sweep_station_liners(station.ports, STATION_ID)

    assert station.liners.reads == 1
    assert len(cleanup_failures(logs)) == 1
    assert stored.exists()
    assert [path for path in orphans if not path.exists()] == []


def test_delete_succeeds_when_the_sweep_read_is_lost(station: Station) -> None:
    """LR3: the delete is committed and its own file removed; the sweep's lost read is one log,
    and the other liner and the orphan stay."""
    deleted = station.seed()
    kept = station.seed()
    orphan = station.orphan()
    station.liners.lost = True

    with capture_logs() as logs:
        delete_liner(station.ports, STATION_ID, UUID(deleted.stem))

    assert UUID(deleted.stem) not in station.liners.liners
    assert not deleted.exists()
    assert kept.exists()
    assert orphan.exists()
    assert len(cleanup_failures(logs)) == 1
    assert station.liners.reads == 1


def test_add_succeeds_when_the_sweep_read_is_lost(station: Station) -> None:
    """LR2: the upload is stored and answered; the sweep's lost read is one log, and no liner
    file is removed."""
    kept = station.seed()
    orphan = station.orphan()
    station.liners.lost = True

    with capture_logs() as logs:
        added = add_liner(station.ports, STATION_ID, ClipUpload(name="ID.wav", data=wav_bytes(0.5)))

    assert added.outcome is UploadOutcome.ADDED
    assert added.liner_id in station.liners.liners
    assert (station.folder / f"{added.liner_id}.wav").exists()
    assert kept.exists()
    assert orphan.exists()
    assert len(cleanup_failures(logs)) == 1
    assert station.liners.reads == 1
