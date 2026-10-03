"""The sign-off routes over HTTP (PR G1, Task 3; traceability H: T3.18-T3.23, T3.29-T3.31).

Requirements: D26 (the user's own sign-off clip, set on the Streaming page, within its
limits); PG3 (FLAC, MP3 or WAV by content; at most 25 MiB; 1 s to 5 min); I2 (the detected
format, not the name, decides; another type is 415); I3 (storage failures answer 503, on the
real ports' commit too, and on DELETE); M3 and the house rule "never skip error handling" (an
upload is bounded: refused from its ``Content-Length`` before the body is read, and, with no
``Content-Length``, refused once the body passes the limit, without reading the rest); PG1
(works while streaming is off); H1 (``X-Airwave-Token`` required); design note 6 and M13 (the
upload writes to the one sign-off folder the service reads). The plan's route table: ``POST
/api/v1/streaming/sign-off`` (multipart ``file``) answers ``{"name", "seconds", "format"}``,
413 / 415 / 422 / 503 on refusal; ``DELETE /api/v1/streaming/sign-off`` answers 204, or 503;
every 422 uses FastAPI's list shape.

The probe is the real one (``probe_clip``); WAVs are made with the stdlib ``wave`` module; an
OGG is ``mutagen.File`` patched to an ``OggVorbis`` (as in ``test_clip_probe.py``). Streamed
bodies count the bytes the server pulls.
"""

from __future__ import annotations

import io
import random
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import mutagen
import psycopg
import pytest
from fastapi import FastAPI
from mutagen.oggvorbis import OggVorbis

from backend.config import get_settings
from backend.dependencies import (
    get_current_token,
    get_sign_off_ports,
    get_streaming_state,
    get_sync_repos,
    get_user_settings,
)
from backend.domain.streaming import MAX_CLIP_BYTES, ClipStorageError, ProbedClip
from backend.routers.v1 import router as v1_router
from backend.services.audio_tags import probe_clip
from backend.services.streaming.sign_off import SignOffPorts, sign_off_folder
from backend.services.streaming.stream_settings import StreamingState
from tests.fakes.user_settings import FakeUserSettingRepository

SIGN_OFF = "/api/v1/streaming/sign-off"
SETTINGS = "/api/v1/streaming/settings"
KEY = "stream_sign_off"
RATE = 8_000
BOUNDARY = "g1-sign-off-boundary"
CHUNK = 2**16
MULTIPART = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}


def wav_bytes(seconds: float) -> bytes:
    """A silent mono 16-bit WAV of exactly ``seconds``."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"\x00\x00" * round(seconds * RATE))
    return buffer.getvalue()


@dataclass
class Clips:
    """The routes' world: a settings fake, a sign-off folder, the real probe (recorded)."""

    folder: Path
    settings: FakeUserSettingRepository = field(default_factory=FakeUserSettingRepository)
    streaming: StreamingState = StreamingState.ON
    commit_error: Exception | None = None
    probed: list[Path] = field(default_factory=list)

    def probe(self, path: Path) -> ProbedClip:
        self.probed.append(path)
        return probe_clip(path)

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error

    def app(self, *, token: bool = True) -> FastAPI:
        app = FastAPI()
        app.include_router(v1_router)
        ports = SignOffPorts(
            settings=self.settings, folder=self.folder, probe=self.probe, commit=self.commit
        )
        app.dependency_overrides[get_sign_off_ports] = lambda: ports
        app.dependency_overrides[get_user_settings] = lambda: self.settings
        app.dependency_overrides[get_streaming_state] = lambda: self.streaming
        if token:
            app.dependency_overrides[get_current_token] = lambda: "test-token"
        return app

    def files(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir()) if self.folder.exists() else []


@pytest.fixture
def clips(tmp_path: Path) -> Clips:
    return Clips(tmp_path / "sign-off")


async def send(
    app: FastAPI, method: str, path: str, upload: tuple[str, bytes] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        if upload is None:
            return await http.request(method, path)
        name, data = upload
        return await http.request(
            method, path, files={"file": (name, data, "application/octet-stream")}
        )


@dataclass
class StreamedUpload:
    """A multipart upload of ``size`` zero bytes, produced only as the server reads it."""

    name: str
    size: int
    pulled: int = 0

    def parts(self) -> tuple[bytes, bytes]:
        head = (
            f"--{BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{self.name}"\r\n'
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


async def send_streamed(
    app: FastAPI, upload: StreamedUpload, *, content_length: bool
) -> httpx.Response:
    """POST ``upload`` as a stream: with its ``Content-Length``, or chunked (none)."""
    headers = dict(MULTIPART)
    if content_length:
        headers["content-length"] = str(upload.length)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.post(SIGN_OFF, content=upload.body(), headers=headers)


def ogg_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mutagen`` detects an Ogg Vorbis file, whatever the bytes and the name."""
    ogg = OggVorbis.__new__(OggVorbis)
    ogg.info = SimpleNamespace(length=3.0, sketchy=False)
    monkeypatch.setattr(mutagen, "File", lambda *args, **kwargs: ogg)


async def test_uploading_a_wav_answers_its_name_length_and_format(clips: Clips) -> None:
    # T3.18 (D26): the real probe reads the WAV; the answer is the page's shape.
    response = await send(clips.app(), "POST", SIGN_OFF, ("signoff.wav", wav_bytes(2.5)))
    assert response.status_code == 200
    assert response.json() == {"name": "signoff.wav", "seconds": 2.5, "format": "wav"}
    assert clips.settings.get(KEY) is not None


REFUSALS = {
    "random-bytes-mp3": 422,
    "content-length-over-limit": 413,
    "part-over-25MiB": 413,
    "half-second-wav": 422,
    "unsupported-type": 415,
    "storage-error": 503,
}


@pytest.mark.parametrize("case", list(REFUSALS))
async def test_a_refused_clip_answers_why(
    clips: Clips, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # T3.19 (PG3, I2, I3, M3; audit MF2): each refusal has its status and a detail (422s in
    # FastAPI's list shape); nothing is stored.
    # - content-length-over-limit: declared over 25 MiB + 1 MiB, so 413 before the body is read
    #   (no more than the first chunk) or anything is probed (M3);
    # - part-over-25MiB: under that declared size, but the file is 25 MiB + 1 byte: 413;
    # - unsupported-type: the content is detected as OGG (named .mp3): 415 (I2);
    # - storage-error: the commit fails (I3): 503.
    app = clips.app()
    status = REFUSALS[case]
    if case == "content-length-over-limit":
        upload = StreamedUpload("huge.wav", MAX_CLIP_BYTES + 2**20 + 1)
        response = await send_streamed(app, upload, content_length=True)
        assert upload.pulled <= CHUNK
    else:
        if case == "unsupported-type":
            ogg_detected(monkeypatch)
        if case == "storage-error":
            clips.commit_error = ClipStorageError("the clip could not be stored: connection lost")
        name, data = {
            "random-bytes-mp3": ("noise.mp3", random.Random(1995).randbytes(64 * 1024)),
            "part-over-25MiB": ("huge.wav", bytes(MAX_CLIP_BYTES + 1)),
            "half-second-wav": ("short.wav", wav_bytes(0.5)),
            "unsupported-type": ("ogg-content.mp3", wav_bytes(2.0)),
            "storage-error": ("ok.wav", wav_bytes(2.0)),
        }[case]
        response = await send(app, "POST", SIGN_OFF, (name, data))
    assert response.status_code == status
    detail = response.json()["detail"]
    if status == 422:
        assert isinstance(detail, list) and detail
        assert all({"loc", "msg", "type"} <= set(entry) for entry in detail)
    else:
        assert isinstance(detail, str) and detail
    if case in ("content-length-over-limit", "part-over-25MiB"):
        assert clips.probed == []
    if case != "storage-error":
        assert clips.settings.get(KEY) is None
        assert [n for n in clips.files() if not n.endswith(".partial")] == []


async def test_a_chunked_upload_over_the_limit_is_413_and_stops_reading(clips: Clips) -> None:
    # T3.30 (D26 and PG3's 25 MiB limit; M3; house rule "never skip error handling"): with no
    # Content-Length, the upload is still bounded: refused once it passes the limit, without
    # reading the rest of the body, and nothing is probed or stored.
    upload = StreamedUpload("chunked.wav", MAX_CLIP_BYTES + 8 * 2**20)
    response = await send_streamed(clips.app(), upload, content_length=False)
    assert response.status_code == 413
    assert isinstance(response.json()["detail"], str)
    assert upload.pulled <= MAX_CLIP_BYTES + 2**20 + 2 * CHUNK
    assert upload.pulled < upload.length
    assert clips.probed == []
    assert clips.settings.get(KEY) is None


async def test_a_wav_named_mp3_is_stored_as_wav(clips: Clips) -> None:
    # T3.20 (I2): the content decides the format and the stored extension.
    response = await send(clips.app(), "POST", SIGN_OFF, ("signoff.mp3", wav_bytes(1.5)))
    assert response.status_code == 200
    assert response.json() == {"name": "signoff.mp3", "seconds": 1.5, "format": "wav"}
    [stored] = clips.files()
    assert stored.endswith(".wav")


async def test_removing_the_clip_is_204_and_the_settings_show_none(clips: Clips) -> None:
    # T3.21 (D26; guard on the shim).
    app = clips.app()
    assert (await send(app, "POST", SIGN_OFF, ("bye.wav", wav_bytes(2.0)))).status_code == 200
    removed = await send(app, "DELETE", SIGN_OFF)
    assert removed.status_code == 204
    read = await send(app, "GET", SETTINGS)
    assert (read.json()["sign_off"], read.json()["sign_off_problem"]) == (None, None)
    assert clips.files() == []


async def test_the_clip_can_be_set_while_streaming_is_off(clips: Clips) -> None:
    # T3.22 (PG1, manual check 8; guard on the shim).
    clips.streaming = StreamingState.OFF
    response = await send(clips.app(), "POST", SIGN_OFF, ("id.wav", wav_bytes(1.0)))
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_the_sign_off_routes_need_the_token(clips: Clips, method: str) -> None:
    # T3.23 (H1; guard on the shim).
    upload = ("id.wav", wav_bytes(1.0)) if method == "POST" else None
    response = await send(clips.app(token=False), method, SIGN_OFF, upload)
    assert response.status_code == 401
    assert clips.settings.get(KEY) is None


@dataclass
class StubRepos:
    """What the real ``get_sign_off_ports`` takes from the request's repositories."""

    user_settings: FakeUserSettingRepository = field(default_factory=FakeUserSettingRepository)
    fail_commit: bool = False

    def commit(self) -> None:
        if self.fail_commit:
            raise psycopg.OperationalError("server closed the connection unexpectedly")


async def test_the_real_ports_use_the_shared_folder_and_a_failed_commit_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T3.29 (I3, design notes 5 and 6, M13; audit SF1): the real ``get_sign_off_ports`` stores
    # the clip in ``sign_off_folder(STREAM_WORK_DIR)``, the folder the service reads (T4.11),
    # and translates a failed database commit into 503, on POST and on DELETE.
    work = tmp_path / "stream-work"
    monkeypatch.setenv("STREAM_WORK_DIR", str(work))
    get_settings.cache_clear()
    repos = StubRepos()
    app = FastAPI()
    app.include_router(v1_router)
    app.dependency_overrides[get_sync_repos] = lambda: repos
    app.dependency_overrides[get_current_token] = lambda: "test-token"
    folder = sign_off_folder(work)
    try:
        stored = await send(app, "POST", SIGN_OFF, ("first.wav", wav_bytes(2.0)))
        landed = [p.suffix for p in folder.iterdir()] if folder.exists() else []
        repos.fail_commit = True
        posted = await send(app, "POST", SIGN_OFF, ("second.wav", wav_bytes(3.0)))
        removed = await send(app, "DELETE", SIGN_OFF)
    finally:
        get_settings.cache_clear()
    assert (stored.status_code, landed) == (200, [".wav"])
    assert (posted.status_code, removed.status_code) == (503, 503)


async def test_an_overlong_upload_name_is_never_a_server_error(clips: Clips) -> None:
    # T3.31 (error handling; audit note 2): the clip's name is kept to SignOff's 1-255
    # characters (trimmed) or the upload is refused (422); never a 500.
    name = "n" * 296 + ".wav"
    response = await send(clips.app(), "POST", SIGN_OFF, (name, wav_bytes(2.0)))
    assert response.status_code in (200, 422)
    if response.status_code == 200:
        assert 1 <= len(response.json()["name"]) <= 255
