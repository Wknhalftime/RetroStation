"""Station liners, PR L1 Task 4: the liner store service, ``backend/services/streaming/
liners.py`` (fakes plus ``tmp_path``).

Requirements: LR2 (any valid audio, no length limit; not-audio refused and nothing kept; the
same audio twice is "already there"), LR3 (stored per station at
``<work_dir>/liners/<station_id>/<liner_id>.<format>``; the display name is the file name
without its extension; rename changes only it; delete removes the row, then the file;
deleting a station's liners removes its folder), LR4 (each liner has an audio hash: FLAC and
MP3 through the library's ``compute_audio_hash``, WAV as ``audio-sha256`` of the whole file,
plan note 5). LR1 for the cadence. Review Focus 2: the same folder uploaded twice leaves one
row and no stray file.

The probe is a port (``ClipProbe``): these tests say what it detects. Hashing reads the real
installed file: the FLAC is built by ``tests/fixtures/audio_builders.write_flac`` with its
STREAMINFO MD5, so its expected hash is ``flac-md5:<pcm_md5>``.
"""

from __future__ import annotations

import hashlib
import io
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from backend.domain.broadcast import BroadcastStation, UnknownStationError
from backend.domain.library import AudioHash
from backend.domain.liners import (
    MAX_LINER_BYTES,
    DuplicateLinerError,
    InvalidLinerValueError,
    LinerCadence,
    LinerNotFoundError,
    LinerTooLargeError,
    StationLiner,
)
from backend.domain.streaming import (
    ClipFormat,
    ProbedClip,
    SignOffError,
    UnreadableClipError,
)
from backend.domain.system import StorageUnavailableError
from backend.services.streaming.liners import (
    LinerAdded,
    LinerAudio,
    LinerPorts,
    UploadOutcome,
    add_liner,
    delete_liner,
    discard_station_liners,
    liner_audio,
    liner_audio_hash,
    liner_folder,
    rename_liner,
    set_liner_cadence,
    station_liner_folder,
)
from backend.services.streaming.sign_off import ClipUpload
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.station_liners import FakeStationLiners
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.fixtures.audio_builders import mp3_frames, pcm_md5, write_flac

STATION_ID = UUID("0a1b2c3d-4e5f-4a6b-9c7d-8e9fa0b1c2d3")
OTHER_STATION_ID = UUID("1b2c3d4e-5f60-4b7c-8d9e-0fa1b2c3d4e5")
FIRST_ID = UUID("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")
SECOND_ID = UUID("7a2d3e4f-5b6c-4d7e-9f80-123456789abc")
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
LEVELS = [(1_000, -1_000), (2_000, -2_000)]


def wav_bytes(seconds: float, *, level: int = 0) -> bytes:
    """A mono 16-bit WAV of ``seconds`` at a constant ``level``."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8_000)
        out.writeframes(level.to_bytes(2, "little", signed=True) * round(seconds * 8_000))
    return buffer.getvalue()


@dataclass
class Store:
    """One liners folder with recording ports: the probe answers ``probed`` (a clip, or a
    refusal it raises), ``new_id`` hands out ``ids`` in turn, the clock is ``NOW``."""

    work: Path
    probed: ProbedClip | SignOffError = field(
        default_factory=lambda: ProbedClip(ClipFormat.FLAC, 7_000)
    )
    ids: list[UUID] = field(default_factory=lambda: [FIRST_ID, SECOND_ID])
    events: list[str] = field(default_factory=list)
    during_commit: Callable[[], None] | None = None
    probe_paths: list[Path] = field(default_factory=list)
    stations: FakeBroadcastStationRepository = field(default_factory=FakeBroadcastStationRepository)
    cues: FakeStreamCueRepository = field(default_factory=FakeStreamCueRepository)
    liners: FakeStationLiners = field(init=False)
    _next: Iterator[UUID] = field(init=False)

    def __post_init__(self) -> None:
        self.liners = FakeStationLiners(self.cues)
        self.stations.create(BroadcastStation(STATION_ID, "WKRP"))
        self.stations.create(BroadcastStation(OTHER_STATION_ID, "WKRQ"))
        self._next = iter(self.ids)

    def probe(self, path: Path) -> ProbedClip:
        self.probe_paths.append(path)
        if isinstance(self.probed, SignOffError):
            raise self.probed
        return self.probed

    def commit(self) -> None:
        self.events.append("commit")
        if self.during_commit is not None:
            self.during_commit()

    def new_id(self) -> UUID:
        return next(self._next)

    @property
    def ports(self) -> LinerPorts:
        return LinerPorts(
            liners=self.liners,
            stations=self.stations,
            folder=liner_folder(self.work),
            probe=self.probe,
            commit=self.commit,
            new_id=self.new_id,
            clock=lambda: NOW,
        )

    @property
    def station_folder(self) -> Path:
        return station_liner_folder(self.work, STATION_ID)

    def add(self, name: str, data: bytes, station_id: UUID = STATION_ID) -> LinerAdded:
        return add_liner(self.ports, station_id, ClipUpload(name=name, data=data))

    def files(self) -> list[str]:
        """Every file under the liners root, as paths relative to it."""
        root = liner_folder(self.work)
        if not root.exists():
            return []
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def seed(
        self, liner_id: UUID, station_id: UUID = STATION_ID, *, data: bytes | None = None
    ) -> StationLiner:
        """A WAV liner already stored: its row in the fake and its file on disk. Its hash is
        ``audio-sha256`` of ``data`` (the file's bytes), as a WAV upload's would be."""
        data = data if data is not None else b"RIFF" + liner_id.bytes
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
        (folder / liner.file_name).write_bytes(data)
        return liner


class RacingLiners(FakeStationLiners):
    """The liner fake as a concurrent upload of the same audio leaves it: the duplicate
    check sees nothing, then ``add`` hits the unique key (the lost race, plan note 15)."""

    def find_by_hash(self, station_id: UUID, audio_hash: AudioHash) -> StationLiner | None:
        return None

    def add(self, liner: StationLiner) -> None:
        if any(
            (x.station_id, x.audio_hash) == (liner.station_id, liner.audio_hash)
            for x in self.liners.values()
        ):
            raise DuplicateLinerError(f"station {liner.station_id} has {liner.audio_hash}")
        super().add(liner)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "work")


@pytest.fixture
def flac(tmp_path: Path) -> bytes:
    return write_flac(tmp_path / "source" / "WKRP ID.flac", LEVELS).read_bytes()


def test_add_stores_file_row_and_hash(store: Store, flac: bytes) -> None:
    """LR2, LR3, LR4: the file lands as ``<id>.flac`` in the station's folder, its row has
    the library's flac-md5 hash, the probe's length and the file name without extension."""
    added = store.add("WKRP ID.flac", flac)

    assert added == LinerAdded(liner_id=FIRST_ID, outcome=UploadOutcome.ADDED)
    installed = store.station_folder / f"{FIRST_ID}.flac"
    assert installed.read_bytes() == flac
    assert store.files() == [f"{STATION_ID}/{FIRST_ID}.flac"]
    assert store.liners.liners[FIRST_ID] == StationLiner(
        id=FIRST_ID,
        station_id=STATION_ID,
        name="WKRP ID",
        file_name=f"{FIRST_ID}.flac",
        format=ClipFormat.FLAC,
        audio_hash=AudioHash.parse(f"flac-md5:{pcm_md5(LEVELS)}"),
        duration_ms=7_000,
        markers=None,
        uploaded_at=NOW,
    )
    assert store.events == ["commit"]
    assert UploadOutcome.ADDED.value == "added"


def test_add_wav_uses_whole_file_sha256(store: Store) -> None:
    """LR4 (plan note 5): a WAV's hash is ``audio-sha256`` of the whole file's bytes."""
    store.probed = ProbedClip(ClipFormat.WAV, 2_500)
    data = wav_bytes(2.5, level=300)
    expected = AudioHash.parse(f"audio-sha256:{hashlib.sha256(data).hexdigest()}")

    store.add("Sweeper.wav", data)

    assert store.liners.liners[FIRST_ID].audio_hash == expected
    assert liner_audio_hash(store.station_folder / f"{FIRST_ID}.wav") == expected


def test_add_mp3_uses_the_library_fingerprint(store: Store) -> None:
    """LR4 (plan note 5): an MP3's hash is the library's: ``audio-sha256`` of its audio bytes
    only, without its ID3v2 and ID3v1 tags, so a re-tagged copy is the same liner."""
    store.probed = ProbedClip(ClipFormat.MP3, 2_000)
    frames = mp3_frames(payload=7)
    data = b"ID3\x03\x00\x00\x00\x00\x00\x00" + frames + b"TAG" + bytes(125)
    expected = AudioHash.parse(f"audio-sha256:{hashlib.sha256(frames).hexdigest()}")

    store.add("More Music.mp3", data)

    assert store.liners.liners[FIRST_ID].audio_hash == expected
    assert store.files() == [f"{STATION_ID}/{FIRST_ID}.mp3"]


def test_duplicate_upload_keeps_one_row_and_no_extra_file(store: Store, flac: bytes) -> None:
    """LR2, LR3, Review Focus 2: the same audio again is "already there": the first liner's
    id, no second row, no second file and no partial left behind."""
    first = store.add("WKRP ID.flac", flac)
    again = store.add("copy of WKRP ID.flac", flac)

    assert first == LinerAdded(liner_id=FIRST_ID, outcome=UploadOutcome.ADDED)
    assert again == LinerAdded(liner_id=FIRST_ID, outcome=UploadOutcome.DUPLICATE)
    assert UploadOutcome.DUPLICATE.value == "duplicate"
    assert list(store.liners.liners) == [FIRST_ID]
    assert store.liners.liners[FIRST_ID].name == "WKRP ID"
    assert store.files() == [f"{STATION_ID}/{FIRST_ID}.flac"]


def test_not_audio_is_refused_and_leaves_nothing(store: Store) -> None:
    """LR2: a file the probe cannot read is refused; no row, no file, no partial."""
    store.probed = UnreadableClipError("the file is not audio that can be read")

    with pytest.raises(UnreadableClipError):
        store.add("cover.jpg", b"\xff\xd8\xff\xe0 not audio")

    assert store.liners.liners == {}
    assert store.files() == []
    assert len(store.probe_paths) == 1
    assert store.events == []


def test_too_large_is_refused_before_staging(store: Store) -> None:
    """LR2 (plan note 13): over ``MAX_LINER_BYTES`` is refused before anything is written
    or probed."""
    with pytest.raises(LinerTooLargeError):
        store.add("huge.wav", bytes(MAX_LINER_BYTES + 1))

    assert store.files() == []
    assert store.probe_paths == []
    assert store.liners.liners == {}


def test_commit_failure_leaves_no_file(store: Store) -> None:
    """LR2, LR3 (Task 4: a failure after install discards the installed file): a commit that
    fails leaves neither the liner's file nor a partial behind."""
    store.probed = ProbedClip(ClipFormat.WAV, 2_000)

    def lost() -> None:
        raise StorageUnavailableError("the connection was lost")

    store.during_commit = lost

    with pytest.raises(StorageUnavailableError):
        store.add("ID.wav", wav_bytes(2.0))

    assert store.files() == []


def test_lost_race_leaves_no_file(store: Store) -> None:
    """LR2, LR3, Review Focus 2 (plan note 15): when ``add`` loses the unique-key race to a
    twin, the upload is a ``DuplicateLinerError`` and only the twin's file stays on disk."""
    store.probed = ProbedClip(ClipFormat.WAV, 2_000)
    data = wav_bytes(2.0, level=40)
    store.liners = RacingLiners(store.cues)
    twin = store.seed(SECOND_ID, data=data)

    with pytest.raises(DuplicateLinerError):
        store.add("ID.wav", data)

    assert list(store.liners.liners) == [twin.id]
    assert store.files() == [f"{STATION_ID}/{SECOND_ID}.wav"]


def test_unknown_station(store: Store, flac: bytes) -> None:
    """LR3: liners belong to a station that exists; nothing is written for another."""
    unknown = uuid4()

    with pytest.raises(UnknownStationError):
        store.add("WKRP ID.flac", flac, station_id=unknown)
    with pytest.raises(UnknownStationError):
        set_liner_cadence(store.ports, unknown, 3)

    assert (store.files(), store.probe_paths, store.liners.liners) == ([], [], {})


@pytest.mark.parametrize("span_ms", [400, 7_200_000], ids=["400 ms", "2 h"])
def test_no_length_limit(store: Store, span_ms: int) -> None:
    """LR2: any valid audio, however short or long."""
    store.probed = ProbedClip(ClipFormat.WAV, span_ms)

    added = store.add("ID.wav", wav_bytes(0.4))

    assert added.outcome is UploadOutcome.ADDED
    assert store.liners.liners[FIRST_ID].duration_ms == span_ms


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("liners/sweepers/More Music.mp3", "More Music"),
        ("", "liner"),
        (" .mp3", "liner"),
        ("n" * 300 + ".wav", "n" * 255),
    ],
    ids=["folders dropped", "no name", "blank stem", "300 characters"],
)
def test_display_name_is_the_file_stem(store: Store, name: str, expected: str) -> None:
    """LR2, LR3: the display name is the upload's name without its extension (or folders);
    with no name, or a blank one, ``"liner"``; a name over 255 characters is cut to 255, so
    valid audio is never refused for its name."""
    store.probed = ProbedClip(ClipFormat.WAV, 2_000)

    store.add(name, wav_bytes(2.0))

    assert store.liners.liners[FIRST_ID].name == expected


def test_rename_changes_only_the_name(store: Store) -> None:
    """LR3: rename changes the display name (trimmed) and nothing else; a blank name is
    refused and changes nothing."""
    liner = store.seed(FIRST_ID)

    rename_liner(store.ports, STATION_ID, FIRST_ID, "  Legal ID ")

    assert store.liners.liners[FIRST_ID] == replace(liner, name="Legal ID")
    assert store.events == ["commit"]
    with pytest.raises(InvalidLinerValueError):
        rename_liner(store.ports, STATION_ID, FIRST_ID, "   ")
    assert store.liners.liners[FIRST_ID].name == "Legal ID"


def test_delete_removes_row_then_file(store: Store) -> None:
    """LR3: delete removes the row and commits, and only then the file."""
    store.seed(FIRST_ID)
    kept = store.seed(SECOND_ID)
    path = store.station_folder / f"{FIRST_ID}.wav"
    seen_at_commit: list[tuple[bool, bool]] = []
    store.during_commit = lambda: seen_at_commit.append(
        (FIRST_ID in store.liners.liners, path.exists())
    )

    delete_liner(store.ports, STATION_ID, FIRST_ID)

    assert seen_at_commit == [(False, True)]
    assert not path.exists()
    assert list(store.liners.liners) == [kept.id]
    assert store.files() == [f"{STATION_ID}/{SECOND_ID}.wav"]


def test_delete_survives_a_file_that_cannot_be_removed(store: Store) -> None:
    """LR3 (Task 4: row, commit, then the file; a file delete failure is logged): a file held
    open (here a directory in its place, so removing it fails on every platform) does not
    undo or fail the delete."""
    store.seed(FIRST_ID)
    path = store.station_folder / f"{FIRST_ID}.wav"
    path.unlink()
    path.mkdir()

    with capture_logs() as logs:
        delete_liner(store.ports, STATION_ID, FIRST_ID)

    assert store.liners.liners == {}
    assert store.events == ["commit"]
    assert [e["event"] for e in logs if e["event"] == "stream_liner_cleanup_failed"] == [
        "stream_liner_cleanup_failed"
    ]


def test_liner_of_another_station_is_not_found(store: Store) -> None:
    """LR3: a liner is reached only through its own station; another station's id, or an
    unknown liner, is not found and changes nothing."""
    liner = store.seed(FIRST_ID, station_id=OTHER_STATION_ID)

    for liner_id, station_id in ((FIRST_ID, STATION_ID), (uuid4(), OTHER_STATION_ID)):
        with pytest.raises(LinerNotFoundError):
            rename_liner(store.ports, station_id, liner_id, "Mine now")
        with pytest.raises(LinerNotFoundError):
            delete_liner(store.ports, station_id, liner_id)
        with pytest.raises(LinerNotFoundError):
            liner_audio(store.ports, station_id, liner_id)

    assert store.liners.liners == {FIRST_ID: liner}
    assert store.files() == [f"{OTHER_STATION_ID}/{FIRST_ID}.wav"]


def test_audio_is_the_liners_own_file(store: Store) -> None:
    """LR3, LR5 (preview): the liner's file and format, found by its id."""
    store.seed(FIRST_ID)

    assert liner_audio(store.ports, STATION_ID, FIRST_ID) == LinerAudio(
        path=store.station_folder / f"{FIRST_ID}.wav", format=ClipFormat.WAV
    )


def test_audio_of_missing_file_is_not_found(store: Store) -> None:
    """LR3: a liner whose file is gone from disk has no audio to serve."""
    store.seed(FIRST_ID)
    (store.station_folder / f"{FIRST_ID}.wav").unlink()

    with pytest.raises(LinerNotFoundError):
        liner_audio(store.ports, STATION_ID, FIRST_ID)


def test_cadence_set_and_clear(store: Store) -> None:
    """LR1: a whole number 1-1,000 is stored on the station; None turns liners off; 0 is
    refused and changes nothing."""
    assert set_liner_cadence(store.ports, STATION_ID, 3) == LinerCadence(3)
    station = store.stations.get_by_id(STATION_ID)
    assert station is not None and station.liner_cadence == LinerCadence(3)

    with pytest.raises(InvalidLinerValueError):
        set_liner_cadence(store.ports, STATION_ID, 0)
    unchanged = store.stations.get_by_id(STATION_ID)
    assert unchanged is not None and unchanged.liner_cadence == LinerCadence(3)

    assert set_liner_cadence(store.ports, STATION_ID, None) is None
    cleared = store.stations.get_by_id(STATION_ID)
    assert cleared is not None and cleared.liner_cadence is None


def test_discard_station_liners_removes_folder(store: Store) -> None:
    """LR3: deleting a station removes its liners' folder, and only that one; a station
    with no folder is not an error."""
    store.seed(FIRST_ID)
    store.seed(SECOND_ID, station_id=OTHER_STATION_ID)
    root = liner_folder(store.work)

    discard_station_liners(root, STATION_ID)
    discard_station_liners(root, uuid4())

    assert not store.station_folder.exists()
    assert store.files() == [f"{OTHER_STATION_ID}/{SECOND_ID}.wav"]
