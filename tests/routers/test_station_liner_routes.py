"""Station liners, PR L1 Task 5: the liner routes over HTTP, under ``/api/v1/stations``.

Requirements: LR1 (the cadence: blank = off, a whole number 1-1,000, strict), LR2 (one file
per upload; any valid audio; not-audio refused, the same audio "already there"; an upload is
bounded by the liner limit, not the sign-off's 25 MiB, and refused from its ``Content-Length``
before the body is read), LR3 (rename, delete, per station). The plan's route table (Task 5)
and its error map (plan note 15): unknown station or another station's liner 404, a lost
duplicate race 409, too large 413, unsupported 415, unreadable 422 at ``["body", "file"]``,
bad value 422, storage failures 503, all 422s in FastAPI's list shape; every route needs
``X-Airwave-Token``.

``get_liner_ports`` is overridden with fakes, ``tmp_path`` and the real content probe
(``probe_clip``), as the sign-off route tests do. The real wiring (``get_liner_ports`` and the
station delete) is tested over PostgreSQL in ``test_station_liner_routes_pg.py``.
"""

from __future__ import annotations

import hashlib
import io
import random
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import mutagen
import pytest
from fastapi import FastAPI
from mutagen.oggvorbis import OggVorbis

from backend.dependencies import get_current_token, get_liner_ports
from backend.domain.broadcast import BroadcastStation
from backend.domain.library import AudioHash
from backend.domain.liners import (
    MAX_LINER_BYTES,
    DuplicateLinerError,
    LinerCadence,
    LinerStorageError,
    StationLiner,
)
from backend.domain.streaming import MAX_CLIP_BYTES, ClipFormat, ProbedClip
from backend.domain.system import StorageUnavailableError
from backend.routers.clip_upload import MULTIPART_SLACK
from backend.routers.v1 import router as v1_router
from backend.services.audio_tags import probe_clip
from backend.services.streaming.liners import LinerPorts, liner_folder, station_liner_folder
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.station_liners import FakeStationLiners
from tests.fakes.stream_cues import FakeStreamCueRepository

STATION_ID = UUID("0a1b2c3d-4e5f-4a6b-9c7d-8e9fa0b1c2d3")
OTHER_STATION_ID = UUID("1b2c3d4e-5f60-4b7c-8d9e-0fa1b2c3d4e5")
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
RATE = 8_000
BOUNDARY = "l1-liner-boundary"
CHUNK = 2**16
MEDIA_TYPES = {
    ClipFormat.FLAC: "audio/flac",
    ClipFormat.MP3: "audio/mpeg",
    ClipFormat.WAV: "audio/wav",
}


def liners_path(station_id: UUID = STATION_ID) -> str:
    return f"/api/v1/stations/{station_id}/liners"


def liner_path(liner_id: UUID, station_id: UUID = STATION_ID) -> str:
    return f"{liners_path(station_id)}/{liner_id}"


def wav_bytes(seconds: float, *, level: int = 0) -> bytes:
    """A mono 16-bit WAV of exactly ``seconds`` at a constant ``level``."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(level.to_bytes(2, "little", signed=True) * round(seconds * RATE))
    return buffer.getvalue()


@dataclass
class Liners:
    """The routes' world: two stations, the liner fake, a liners folder, the real probe
    (recorded), and a commit that raises ``commit_error`` when one is set."""

    work: Path
    stations: FakeBroadcastStationRepository = field(default_factory=FakeBroadcastStationRepository)
    cues: FakeStreamCueRepository = field(default_factory=FakeStreamCueRepository)
    probed: list[Path] = field(default_factory=list)
    commit_error: Exception | None = None
    liners: FakeStationLiners = field(init=False)

    def __post_init__(self) -> None:
        self.liners = FakeStationLiners(self.cues)
        self.stations.create(BroadcastStation(STATION_ID, "WKRP"))
        self.stations.create(BroadcastStation(OTHER_STATION_ID, "WKRQ"))

    def probe(self, path: Path) -> ProbedClip:
        self.probed.append(path)
        return probe_clip(path)

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error

    def app(self, *, token: bool = True) -> FastAPI:
        app = FastAPI()
        app.include_router(v1_router)
        ports = LinerPorts(
            liners=self.liners,
            stations=self.stations,
            folder=liner_folder(self.work),
            probe=self.probe,
            commit=self.commit,
            clock=lambda: NOW,
        )
        app.dependency_overrides[get_liner_ports] = lambda: ports
        if token:
            app.dependency_overrides[get_current_token] = lambda: "test-token"
        return app

    def seed(
        self,
        station_id: UUID = STATION_ID,
        fmt: ClipFormat = ClipFormat.WAV,
        *,
        data: bytes | None = None,
    ) -> StationLiner:
        """A liner already stored: its row in the fake and its file (``data``) on disk, its
        hash ``audio-sha256`` of those bytes, as a WAV upload's would be."""
        liner_id = uuid4()
        data = data if data is not None else f"audio of {liner_id}".encode()
        liner = StationLiner(
            id=liner_id,
            station_id=station_id,
            name="Sweeper",
            file_name=f"{liner_id}.{fmt}",
            format=fmt,
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

    def files(self) -> list[str]:
        root = liner_folder(self.work)
        if not root.exists():
            return []
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


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
def world(tmp_path: Path) -> Liners:
    return Liners(tmp_path / "work")


async def send(
    app: FastAPI,
    method: str,
    path: str,
    *,
    body: object = None,
    upload: tuple[str, bytes] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        if upload is not None:
            name, data = upload
            files = {"file": (name, data, "application/octet-stream")}
            return await http.request(method, path, files=files)
        if body is not None:
            return await http.request(method, path, json=body)
        return await http.request(method, path)


def assert_list_shape(response: httpx.Response) -> list[dict[str, object]]:
    """A 422 in FastAPI's list shape; its entries."""
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert all({"loc", "msg", "type"} <= set(entry) for entry in detail)
    return detail


def ogg_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mutagen`` detects an Ogg Vorbis file, whatever the bytes and the name."""
    ogg = OggVorbis.__new__(OggVorbis)
    ogg.info = SimpleNamespace(length=3.0, sketchy=False)
    monkeypatch.setattr(mutagen, "File", lambda *args, **kwargs: ogg)


async def test_list_shape(world: Liners) -> None:
    """LR1, LR5: the cadence and the station's liners, each with exactly the table's keys."""
    world.stations.set_liner_cadence(STATION_ID, LinerCadence(4))
    liner = world.seed()
    world.seed(OTHER_STATION_ID)

    response = await send(world.app(), "GET", liners_path())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"songs_between_liners", "liners"}
    assert body["songs_between_liners"] == 4
    [entry] = body["liners"]
    assert set(entry) == {"id", "name", "format", "duration_ms", "cues", "uploaded_at"}
    assert {k: v for k, v in entry.items() if k != "uploaded_at"} == {
        "id": str(liner.id),
        "name": "Sweeper",
        "format": "wav",
        "duration_ms": 4_000,
        "cues": "analysing",
    }
    assert datetime.fromisoformat(entry["uploaded_at"]) == NOW


async def test_cadence_put_and_clear(world: Liners) -> None:
    """LR1: a cadence is stored and answered in the list's full shape (the GET's answer, its
    liners included); null turns liners off."""
    world.seed()
    app = world.app()

    put = await send(app, "PUT", f"{liners_path()}/cadence", body={"songs_between_liners": 3})
    station = world.stations.get_by_id(STATION_ID)
    stored = None if station is None else station.liner_cadence  # read now: the fake aliases
    listed = await send(app, "GET", liners_path())
    cleared = await send(
        app, "PUT", f"{liners_path()}/cadence", body={"songs_between_liners": None}
    )

    assert put.status_code == 200
    assert put.json()["songs_between_liners"] == 3
    assert len(put.json()["liners"]) == 1
    assert put.json() == listed.json()
    assert stored == LinerCadence(3)
    assert cleared.status_code == 200
    assert cleared.json() == {**listed.json(), "songs_between_liners": None}
    after = world.stations.get_by_id(STATION_ID)
    assert after is not None and after.liner_cadence is None


@pytest.mark.parametrize("value", [0, 1001])
async def test_cadence_out_of_range_is_422(world: Liners, value: int) -> None:
    """LR1: outside 1-1,000 is refused in the list shape, and nothing changes."""
    world.stations.set_liner_cadence(STATION_ID, LinerCadence(4))

    response = await send(
        world.app(), "PUT", f"{liners_path()}/cadence", body={"songs_between_liners": value}
    )

    assert_list_shape(response)
    station = world.stations.get_by_id(STATION_ID)
    assert station is not None and station.liner_cadence == LinerCadence(4)


@pytest.mark.parametrize("value", ["3", 2.5, True])
async def test_cadence_non_int_is_422(world: Liners, value: object) -> None:
    """LR1: strict whole numbers only; a string, a fraction or a boolean is refused."""
    response = await send(
        world.app(), "PUT", f"{liners_path()}/cadence", body={"songs_between_liners": value}
    )

    assert_list_shape(response)
    station = world.stations.get_by_id(STATION_ID)
    assert station is not None and station.liner_cadence is None


async def test_upload_added_201(world: Liners) -> None:
    """LR2, LR3: one file uploaded is added (201), stored, and listed under its name."""
    app = world.app()

    response = await send(app, "POST", liners_path(), upload=("WKRP ID.wav", wav_bytes(2.5)))

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "outcome"} and body["outcome"] == "added"
    liner_id = UUID(body["id"])
    assert world.files() == [f"{STATION_ID}/{liner_id}.wav"]
    listed = (await send(app, "GET", liners_path())).json()["liners"]
    assert [(e["id"], e["name"], e["format"], e["duration_ms"]) for e in listed] == [
        (str(liner_id), "WKRP ID", "wav", 2_500)
    ]


async def test_upload_bigger_than_a_sign_off_clip_is_stored_whole(world: Liners) -> None:
    """LR2 (spec: "the sign-off's 25 MiB is too small for long WAVs"; plan note 13): the
    liner limit, not the sign-off's, bounds an upload, and the file is stored whole."""
    data = wav_bytes(1_800.0)  # 8 kHz mono 16-bit: about 28.8 MB
    assert len(data) > MAX_CLIP_BYTES + MULTIPART_SLACK

    response = await send(world.app(), "POST", liners_path(), upload=("Long.wav", data))

    assert response.status_code == 201
    assert response.json()["outcome"] == "added"
    liner_id = UUID(response.json()["id"])
    stored = station_liner_folder(world.work, STATION_ID) / f"{liner_id}.wav"
    assert stored.read_bytes() == data


async def test_upload_duplicate_201_duplicate(world: Liners) -> None:
    """LR2: the same audio again is 201 "duplicate", with the existing liner's id."""
    app = world.app()
    data = wav_bytes(2.0, level=500)

    first = await send(app, "POST", liners_path(), upload=("ID.wav", data))
    again = await send(app, "POST", liners_path(), upload=("ID copy.wav", data))

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == {"id": first.json()["id"], "outcome": "duplicate"}
    assert len(world.liners.liners) == 1
    assert len(world.files()) == 1


async def test_upload_unsupported_415(world: Liners, monkeypatch: pytest.MonkeyPatch) -> None:
    """LR2: audio of a type the stream cannot play (OGG here) is 415; nothing is kept."""
    ogg_detected(monkeypatch)

    response = await send(world.app(), "POST", liners_path(), upload=("id.ogg", b"OggS..."))

    assert response.status_code == 415
    assert isinstance(response.json()["detail"], str)
    assert (world.liners.liners, world.files()) == ({}, [])


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("noise.mp3", random.Random(1995).randbytes(64 * 1024)),
        ("cover.jpg", b"\xff\xd8\xff\xe0" + bytes(64)),
        ("notes.txt", b"notes"),
        ("side.cue", b'FILE "a.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'),
    ],
    ids=["random bytes", "jpeg", "text", "cue sheet"],
)
async def test_upload_unreadable_422(world: Liners, name: str, data: bytes) -> None:
    """LR2, Review Focus 1: a file that is not audio (random bytes, a cover, notes, a cue
    sheet) is 422 at ``["body", "file"]``; nothing is kept."""
    response = await send(world.app(), "POST", liners_path(), upload=(name, data))

    detail = assert_list_shape(response)
    assert [entry["loc"] for entry in detail] == [["body", "file"]]
    assert (world.liners.liners, world.files()) == ({}, [])


async def test_upload_lost_race_is_409(world: Liners) -> None:
    """LR2 (plan note 15): an upload that loses the unique-key race to the same audio is 409
    with a reason; only the twin stays, row and file."""
    data = wav_bytes(2.0, level=40)
    world.liners = RacingLiners(world.cues)
    twin = world.seed(data=data)

    response = await send(world.app(), "POST", liners_path(), upload=("ID.wav", data))

    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)
    assert list(world.liners.liners) == [twin.id]
    assert world.files() == [f"{STATION_ID}/{twin.file_name}"]


@pytest.mark.parametrize(
    "error",
    [LinerStorageError("the liner could not be stored"), StorageUnavailableError("lost")],
    ids=["liner storage", "storage unavailable"],
)
async def test_storage_failure_is_503(world: Liners, error: Exception) -> None:
    """LR1, LR2 (plan note 15): a storage failure answers 503 with a reason, on an upload and
    on a cadence change."""
    world.commit_error = error
    app = world.app()

    upload = await send(app, "POST", liners_path(), upload=("ID.wav", wav_bytes(1.0)))
    cadence = await send(app, "PUT", f"{liners_path()}/cadence", body={"songs_between_liners": 3})

    for response in (upload, cadence):
        assert response.status_code == 503
        assert isinstance(response.json()["detail"], str)


@dataclass
class StreamedUpload:
    """A multipart upload of ``size`` zero bytes, produced only as the server reads it."""

    size: int
    pulled: int = 0

    def parts(self) -> tuple[bytes, bytes]:
        head = (
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="file"; filename="huge.wav"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        return head, f"\r\n--{BOUNDARY}--\r\n".encode()

    @property
    def length(self) -> int:
        head, tail = self.parts()
        return len(head) + self.size + len(tail)

    async def body(self) -> AsyncIterator[bytes]:
        head, tail = self.parts()
        self.pulled += len(head)
        yield head
        left = self.size
        while left:
            step = min(CHUNK, left)
            left -= step
            self.pulled += step
            yield bytes(step)
        self.pulled += len(tail)
        yield tail


async def test_upload_declared_too_large_413_before_reading(world: Liners) -> None:
    """LR2 (plan note 13): a ``Content-Length`` over the liner limit plus the multipart slack
    is 413 before the body is read (no more than its first chunk) or anything is probed."""
    upload = StreamedUpload(MAX_LINER_BYTES + MULTIPART_SLACK + 1)
    headers = {
        "content-type": f"multipart/form-data; boundary={BOUNDARY}",
        "content-length": str(upload.length),
    }
    transport = httpx.ASGITransport(app=world.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.post(liners_path(), content=upload.body(), headers=headers)

    assert response.status_code == 413
    assert isinstance(response.json()["detail"], str)
    assert upload.pulled <= CHUNK
    assert world.probed == []
    assert (world.liners.liners, world.files()) == ({}, [])


async def test_rename_204_and_blank_name_422(world: Liners) -> None:
    """LR3: rename answers 204 and changes the name; a blank name is 422 and changes
    nothing."""
    liner = world.seed()
    app = world.app()

    renamed = await send(app, "PATCH", liner_path(liner.id), body={"name": "Legal ID"})
    blank = await send(app, "PATCH", liner_path(liner.id), body={"name": "   "})

    assert renamed.status_code == 204
    assert_list_shape(blank)
    assert world.liners.liners[liner.id].name == "Legal ID"


async def test_delete_204(world: Liners) -> None:
    """LR3: delete answers 204; the row and the file are gone."""
    liner = world.seed()
    app = world.app()

    response = await send(app, "DELETE", liner_path(liner.id))

    assert response.status_code == 204
    assert (world.liners.liners, world.files()) == ({}, [])
    assert (await send(app, "GET", liners_path())).json()["liners"] == []


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [("PATCH", "", {"name": "Mine now"}), ("DELETE", "", None), ("GET", "/audio", None)],
)
async def test_other_stations_liner_404(
    world: Liners, method: str, suffix: str, body: object
) -> None:
    """LR3: another station's liner is not found through this station; it is unchanged."""
    liner = world.seed(OTHER_STATION_ID)

    response = await send(world.app(), method, liner_path(liner.id) + suffix, body=body)

    assert response.status_code == 404
    assert world.liners.liners == {liner.id: liner}
    assert world.files() == [f"{OTHER_STATION_ID}/{liner.file_name}"]


@pytest.mark.parametrize(
    ("method", "suffix", "body", "upload"),
    [
        ("GET", "", None, None),
        ("PUT", "/cadence", {"songs_between_liners": 3}, None),
        ("POST", "", None, ("ID.wav", wav_bytes(1.0))),
    ],
    ids=["list", "cadence", "upload"],
)
async def test_unknown_station_404(
    world: Liners,
    method: str,
    suffix: str,
    body: object,
    upload: tuple[str, bytes] | None,
) -> None:
    """LR3: a station that does not exist is 404, and nothing is stored."""
    path = liners_path(uuid4()) + suffix

    response = await send(world.app(), method, path, body=body, upload=upload)

    assert response.status_code == 404
    assert (world.liners.liners, world.files()) == ({}, [])


@pytest.mark.parametrize("fmt", list(ClipFormat))
async def test_audio_serves_bytes_with_media_type(world: Liners, fmt: ClipFormat) -> None:
    """LR5 (preview): the liner's own file, with its format's media type."""
    liner = world.seed(fmt=fmt)
    stored = (station_liner_folder(world.work, STATION_ID) / liner.file_name).read_bytes()

    response = await send(world.app(), "GET", f"{liner_path(liner.id)}/audio")

    assert response.status_code == 200
    assert response.content == stored
    assert response.headers["content-type"] == MEDIA_TYPES[fmt]


@pytest.mark.parametrize(
    ("method", "suffix", "body", "upload"),
    [
        ("GET", "", None, None),
        ("PUT", "/cadence", {"songs_between_liners": 3}, None),
        ("POST", "", None, ("ID.wav", wav_bytes(1.0))),
        ("PATCH", "/{liner}", {"name": "Legal ID"}, None),
        ("DELETE", "/{liner}", None, None),
        ("GET", "/{liner}/audio", None, None),
    ],
    ids=["list", "cadence", "upload", "rename", "delete", "audio"],
)
async def test_token_required(
    world: Liners,
    method: str,
    suffix: str,
    body: object,
    upload: tuple[str, bytes] | None,
) -> None:
    """LR1-LR3 (H1): every liner route needs ``X-Airwave-Token``; nothing changes without."""
    liner = world.seed()
    path = liners_path() + suffix.format(liner=liner.id)

    response = await send(world.app(token=False), method, path, body=body, upload=upload)

    assert response.status_code == 401
    assert world.liners.liners == {liner.id: liner}
    station = world.stations.get_by_id(STATION_ID)
    assert station is not None and station.liner_cadence is None
