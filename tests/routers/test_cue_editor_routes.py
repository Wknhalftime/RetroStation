"""The cue editor routes over HTTP (cue editor PR 2, Task 8; R7, Review Focus 4).

Requirements: R7 (view, save, reset, waveform and audio, token required, the error mapping of
plan note 11). Everything is under ``/api/v1/library``, needs ``X-Airwave-Token``, and is a plain
``def`` (the repositories are synchronous). The plan's route table:

    GET    /files/{file_id}/cues      200 {file_id, title, artist, format, duration_ms,
                                          analysis, automatic, manual}
    PUT    /files/{file_id}/cues      body {start_ms, eom_ms, fade_at_ms, stop_ms}, 204
    DELETE /files/{file_id}/cues      204
    GET    /files/{file_id}/waveform  200 {duration_ms, per_second, peaks (base64)}
    GET    /files/{file_id}/audio     200, the file, with its media type

Errors: no such file or not present or gone from disk 404; no audio hash or no length 409; a
refused edit or unreadable audio 422 (the edit in FastAPI's list shape, ``loc: ["body"]``); a
lost database 503 ``"unavailable"``. The service is faked through ``app.dependency_overrides``.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.config import get_settings
from backend.dependencies import (
    get_cue_editor_ports,
    get_current_token,
    get_peak_reader,
    get_stream_timing,
)
from backend.domain.enums import FileStatus
from backend.domain.library import AudioHash, AudioMetadata, LibraryFile
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueMarkers,
    CuePoints,
    StreamTiming,
)
from backend.domain.system import StorageUnavailableError
from backend.playout.peaks import PeakReadError, Peaks
from backend.routers.v1 import router as v1_router
from backend.services.streaming.cue_editor import CueEditorPorts
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.stream_cue_edits import FakeStreamCueEditRepository
from tests.fakes.stream_cues import FakeStreamCueRepository

LIBRARY = "/api/v1/library"
FILE_ID = UUID("00000000-0000-0000-0000-0000000000a1")
HASH = AudioHash.parse("flac-md5:" + "a" * 32)
HERETIC = CuePoints(
    cue_in_ms=0,
    cue_out_ms=247_100,
    fade_in_ms=0,
    fade_out_ms=6_000,
    start_next_ms=6_000,
    gain_db=-11.63,
)
EDIT = {"start_ms": 9_000, "eom_ms": 241_100, "fade_at_ms": 244_000, "stop_ms": 247_100}
EDIT_MARKERS = CueMarkers(start_ms=9_000, eom_ms=241_100, fade_at_ms=244_000, stop_ms=247_100)
NOT_FINGERPRINTED = "This file hasn't been fingerprinted yet, so its cues can't be edited."
AUDIO_BYTES = bytes(range(256)) * 8

Body = dict[str, object]
Route = tuple[str, str, Body | None]


def cues_path(file_id: UUID = FILE_ID) -> str:
    return f"{LIBRARY}/files/{file_id}/cues"


def route_table(file_id: UUID = FILE_ID) -> list[Route]:
    """The five routes, each with a body that would be valid if the file were editable."""
    return [
        ("GET", cues_path(file_id), None),
        ("PUT", cues_path(file_id), EDIT),
        ("DELETE", cues_path(file_id), None),
        ("GET", f"{LIBRARY}/files/{file_id}/waveform", None),
        ("GET", f"{LIBRARY}/files/{file_id}/audio", None),
    ]


ROUTE_IDS = ["get-cues", "put-cues", "delete-cues", "waveform", "audio"]


@dataclass
class Rig:
    files: FakeLibraryFileRepository
    cues: FakeStreamCueRepository
    edits: FakeStreamCueEditRepository
    path: Path

    @property
    def ports(self) -> CueEditorPorts:
        return CueEditorPorts(files=self.files, cues=self.cues, edits=self.edits)

    def add_file(self, *, file_status: FileStatus = FileStatus.PRESENT, **changes: Any) -> None:
        """Store the Heretic Anthem FLAC (not on disk) with some fields changed; the fake's
        upsert marks a row present, so a missing file is marked missing afterwards."""
        row = LibraryFile(
            id=FILE_ID,
            file_path=str(self.path),
            format="flac",
            audio_hash=HASH,
            audio=AudioMetadata(
                track_title="The Heretic Anthem", artist_name="Slipknot", duration_ms=252_893
            ),
        )
        self.files.upsert(dataclasses.replace(row, **changes))
        if file_status == FileStatus.MISSING:
            self.files.mark_missing(str(self.path))

    def measure(self) -> None:
        self.cues.upsert(CueAnalysis(HASH, HERETIC, -14.0, False, CUE_ANALYSER_VERSION))

    def put_on_disk(self, content: bytes = AUDIO_BYTES) -> None:
        self.path.write_bytes(content)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    built = Rig(
        FakeLibraryFileRepository(),
        FakeStreamCueRepository(),
        FakeStreamCueEditRepository(),
        tmp_path / "heretic.flac",
    )
    built.add_file()
    return built


def read_two_peaks(path: str) -> Peaks:
    return Peaks(200, b"\x00\xff")


def app_for(
    ports: CueEditorPorts,
    reader: Callable[[str], Peaks] = read_two_peaks,
    *,
    token: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.include_router(v1_router)
    app.dependency_overrides[get_cue_editor_ports] = lambda: ports
    app.dependency_overrides[get_peak_reader] = lambda: reader
    app.dependency_overrides[get_stream_timing] = lambda: StreamTiming()
    if token:
        app.dependency_overrides[get_current_token] = lambda: "test-token"
    return app


async def call(
    app: FastAPI,
    method: str,
    path: str,
    body: object = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        if body is None:
            return await http.request(method, path, headers=headers)
        return await http.request(method, path, json=body, headers=headers)


def assert_list_shape(response: httpx.Response) -> list[dict[str, Any]]:
    """FastAPI's 422 shape: ``detail`` is a non-empty list of {loc, msg, type}."""
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert all({"loc", "msg", "type"} <= set(entry) for entry in detail)
    return detail


# --- The route table (R7) -----------------------------------------------------------------


def test_the_five_routes_exist_and_are_plain_defs(rig: Rig) -> None:
    """R7: exactly the table's methods and paths under /api/v1/library, none of them
    ``async def`` (the repositories are synchronous and would block the event loop)."""
    found = {
        (method, route.path): route.endpoint
        for route in app_for(rig.ports).routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    wanted = {
        ("GET", f"{LIBRARY}/files/{{file_id}}/cues"),
        ("PUT", f"{LIBRARY}/files/{{file_id}}/cues"),
        ("DELETE", f"{LIBRARY}/files/{{file_id}}/cues"),
        ("GET", f"{LIBRARY}/files/{{file_id}}/waveform"),
        ("GET", f"{LIBRARY}/files/{{file_id}}/audio"),
    }
    assert wanted <= set(found)
    assert not [key for key in wanted if inspect.iscoroutinefunction(found[key])]


# --- GET the view -------------------------------------------------------------------------


async def test_get_view_shape(rig: Rig) -> None:
    """R7, R3: the view's keys are exactly the table's, and a measured song's automatic markers
    are autocue's as the editor opens on them."""
    rig.measure()
    response = await call(app_for(rig.ports), "GET", cues_path())
    assert response.status_code == 200
    assert response.json() == {
        "file_id": str(FILE_ID),
        "title": "The Heretic Anthem",
        "artist": "Slipknot",
        "format": "flac",
        "duration_ms": 252_893,
        "analysis": "measured",
        "automatic": {
            "start_ms": 0,
            "eom_ms": 241_100,
            "fade_at_ms": 241_100,
            "stop_ms": 247_100,
        },
        "manual": None,
    }


@pytest.mark.parametrize("state", ["measured", "failed", "waiting"])
async def test_get_view_says_how_the_song_was_analysed(rig: Rig, state: str) -> None:
    """R7, R3, C5: ``analysis`` is measured, failed or waiting; the last two open on the whole
    file."""
    if state == "measured":
        rig.measure()
    elif state == "failed":
        fallback = CuePoints(0, 252_893, 0, 4_000, 0, -8.0)
        rig.cues.upsert(CueAnalysis(HASH, fallback, None, True, CUE_ANALYSER_VERSION))
    body = (await call(app_for(rig.ports), "GET", cues_path())).json()
    assert body["analysis"] == state
    if state != "measured":
        assert body["automatic"] == {
            "start_ms": 0,
            "eom_ms": 252_893,
            "fade_at_ms": 248_893,
            "stop_ms": 252_893,
        }


async def test_get_view_shows_the_saved_edit_as_manual(rig: Rig) -> None:
    """R7, R4: a stored edit is ``manual``, in the Markers keys."""
    rig.edits.save(HASH, EDIT_MARKERS)
    body = (await call(app_for(rig.ports), "GET", cues_path())).json()
    assert body["manual"] == EDIT


# --- PUT save -----------------------------------------------------------------------------


async def test_put_saves_and_answers_204(rig: Rig) -> None:
    """R7, R4: a valid edit answers 204 with no body, is stored by audio hash, and the view then
    shows it as manual."""
    app = app_for(rig.ports)
    response = await call(app, "PUT", cues_path(), EDIT)
    assert response.status_code == 204
    assert response.content == b""
    assert rig.edits.edits == {HASH: EDIT_MARKERS}
    assert (await call(app, "GET", cues_path())).json()["manual"] == EDIT


@pytest.mark.parametrize(
    "changes",
    [
        {"eom_ms": 9_000},  # eom == start
        {"eom_ms": 8_000},  # eom before start
        {"eom_ms": 250_000},  # eom after stop
        {"fade_at_ms": 8_000},  # fade before start
        {"fade_at_ms": 250_000},  # fade after stop
    ],
    ids=["eom-at-start", "eom-before-start", "eom-after-stop", "fade-early", "fade-late"],
)
async def test_put_bad_order_is_422(rig: Rig, changes: Body) -> None:
    """R1, R7: markers that break ``start < eom <= stop`` or ``start <= fade <= stop`` answer
    422 in FastAPI's list shape with ``loc == ["body"]``, and nothing is stored."""
    response = await call(app_for(rig.ports), "PUT", cues_path(), {**EDIT, **changes})
    detail = assert_list_shape(response)
    assert detail[0]["loc"] == ["body"]
    assert detail[0]["msg"]
    assert rig.edits.edits == {}


async def test_put_refused_by_rule_is_422_with_reason(rig: Rig) -> None:
    """R6, C4, R7: markers that are in order but that the radio would refuse (an 8 s fade needs
    11 s of a 10 s song) answer 422, in the list shape, and the reason names the 3 s rule."""
    squeezed = {"start_ms": 0, "eom_ms": 9_000, "fade_at_ms": 2_000, "stop_ms": 10_000}
    detail = assert_list_shape(await call(app_for(rig.ports), "PUT", cues_path(), squeezed))
    assert detail[0]["loc"] == ["body"]
    assert "3 s" in detail[0]["msg"]
    assert rig.edits.edits == {}


async def test_put_past_the_end_of_file_is_422(rig: Rig) -> None:
    """R6, R7: a Stop beyond the file's 252_893 ms answers 422 with a reason, and stores nothing."""
    past = {"start_ms": 0, "eom_ms": 252_894, "fade_at_ms": 248_894, "stop_ms": 252_894}
    detail = assert_list_shape(await call(app_for(rig.ports), "PUT", cues_path(), past))
    assert detail[0]["loc"] == ["body"]
    assert rig.edits.edits == {}


@pytest.mark.parametrize(
    "change",
    [
        {"start_ms": "9000"},
        {"start_ms": 9_000.5},
        {"start_ms": 9_000.0},
        {"start_ms": True},
        {"start_ms": None},
        {"stop_ms": "247100"},
        {"start_ms": -1},
    ],
    ids=["string", "fraction", "whole-float", "bool", "null", "string-stop", "negative"],
)
async def test_put_non_int_is_422(rig: Rig, change: Body) -> None:
    """R7: every marker is a strict whole number >= 0: a string, a float, a bool, a null or a
    negative is 422 (FastAPI's list shape), and nothing is stored."""
    response = await call(app_for(rig.ports), "PUT", cues_path(), {**EDIT, **change})
    assert_list_shape(response)
    assert rig.edits.edits == {}


async def test_put_missing_marker_is_422(rig: Rig) -> None:
    """R7: all four markers are required."""
    partial = {key: value for key, value in EDIT.items() if key != "fade_at_ms"}
    assert_list_shape(await call(app_for(rig.ports), "PUT", cues_path(), partial))
    assert rig.edits.edits == {}


# --- DELETE reset -------------------------------------------------------------------------


async def test_delete_answers_204_and_clears(rig: Rig) -> None:
    """R7, R4: reset answers 204 with no body, removes the edit, and the view shows no manual."""
    rig.edits.save(HASH, EDIT_MARKERS)
    app = app_for(rig.ports)
    response = await call(app, "DELETE", cues_path())
    assert response.status_code == 204
    assert response.content == b""
    assert rig.edits.edits == {}
    assert (await call(app, "GET", cues_path())).json()["manual"] is None


async def test_delete_without_an_edit_is_204(rig: Rig) -> None:
    """R7, R4: resetting a song that has no edit is not an error."""
    assert (await call(app_for(rig.ports), "DELETE", cues_path())).status_code == 204


# --- Files that cannot be edited ----------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), route_table(uuid4()), ids=ROUTE_IDS)
async def test_unknown_file_404(rig: Rig, method: str, path: str, body: Body | None) -> None:
    """R7, note 11: a file id that is not in the library is 404 on every route."""
    assert (await call(app_for(rig.ports), method, path, body)).status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), route_table(), ids=ROUTE_IDS)
async def test_a_file_that_is_not_present_is_404(
    rig: Rig, method: str, path: str, body: Body | None
) -> None:
    """R7, note 11: a row whose file_status is missing is 404 on every route."""
    rig.put_on_disk()
    rig.add_file(file_status=FileStatus.MISSING)
    assert (await call(app_for(rig.ports), method, path, body)).status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), route_table()[:3], ids=ROUTE_IDS[:3])
async def test_unhashed_409_message(rig: Rig, method: str, path: str, body: Body | None) -> None:
    """R4, R7, note 11: a present file with no audio hash is 409, and ``detail`` is the sentence
    the page shows; nothing is stored."""
    rig.add_file(audio_hash=None)
    response = await call(app_for(rig.ports), method, path, body)
    assert response.status_code == 409
    assert response.json() == {"detail": NOT_FINGERPRINTED}
    assert rig.edits.edits == {}


@pytest.mark.parametrize(("method", "path", "body"), route_table()[:3], ids=ROUTE_IDS[:3])
async def test_a_file_without_a_length_is_409(
    rig: Rig, method: str, path: str, body: Body | None
) -> None:
    """R6, R7, note 11: a file with no known length is 409 with its own sentence."""
    rig.add_file(audio=AudioMetadata(track_title="Mystery", duration_ms=None))
    response = await call(app_for(rig.ports), method, path, body)
    assert response.status_code == 409
    assert response.json() == {"detail": "This file has no known length."}


# --- Waveform -----------------------------------------------------------------------------


async def test_waveform_base64(rig: Rig) -> None:
    """R7: the waveform's peaks go out as base64 of the bytes (b"\\x00\\xff" is "AP8="), with the
    file's length and the reader's rate, and the reader is given the file's path."""
    rig.put_on_disk()
    asked: list[str] = []

    def reader(path: str) -> Peaks:
        asked.append(path)
        return Peaks(200, b"\x00\xff")

    response = await call(app_for(rig.ports, reader), "GET", f"{LIBRARY}/files/{FILE_ID}/waveform")
    assert response.status_code == 200
    assert response.json() == {"duration_ms": 252_893, "per_second": 200, "peaks": "AP8="}
    assert base64.b64decode(response.json()["peaks"]) == b"\x00\xff"
    assert asked == [str(rig.path)]


async def test_waveform_unreadable_422(rig: Rig) -> None:
    """R7, note 11: audio FFmpeg cannot read is 422, not a 500."""
    rig.put_on_disk()

    def reader(path: str) -> Peaks:
        raise PeakReadError("Invalid data found when processing input")

    response = await call(app_for(rig.ports, reader), "GET", f"{LIBRARY}/files/{FILE_ID}/waveform")
    assert response.status_code == 422


# --- Audio --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_format", "media_type"),
    [
        ("flac", "audio/flac"),
        ("mp3", "audio/mpeg"),
        ("wav", "audio/wav"),
        ("m4a", "audio/mp4"),
        ("ogg", "audio/ogg"),
        ("wma", "application/octet-stream"),
    ],
)
async def test_audio_route_serves_bytes_with_media_type(
    rig: Rig, tmp_path: Path, file_format: str, media_type: str
) -> None:
    """R7: the file's bytes, whole, with the media type of its format; a format the table does
    not know is application/octet-stream."""
    rig.path = tmp_path / f"song.{file_format}"
    rig.add_file(format=file_format)
    rig.put_on_disk()
    response = await call(app_for(rig.ports), "GET", f"{LIBRARY}/files/{FILE_ID}/audio")
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.content == AUDIO_BYTES


async def test_audio_route_missing_on_disk_404(rig: Rig) -> None:
    """Review Focus 4, R7: the row says present but the file is gone: 404 on the audio route,
    not a 500."""
    assert not rig.path.exists()
    response = await call(app_for(rig.ports), "GET", f"{LIBRARY}/files/{FILE_ID}/audio")
    assert response.status_code == 404


async def test_waveform_route_missing_on_disk_404(rig: Rig) -> None:
    """Review Focus 4, R7: the same for the waveform, and the reader is never asked."""

    def reader(path: str) -> Peaks:
        raise AssertionError(f"the reader must not run for a file that is gone ({path})")

    response = await call(app_for(rig.ports, reader), "GET", f"{LIBRARY}/files/{FILE_ID}/waveform")
    assert response.status_code == 404


# --- A lost database ----------------------------------------------------------------------


class LostFiles(FakeLibraryFileRepository):
    def get_by_id(self, file_id: UUID) -> LibraryFile | None:
        raise StorageUnavailableError("the connection was lost")


class LostEdits(FakeStreamCueEditRepository):
    def get(self, audio_hash: AudioHash) -> CueMarkers | None:
        raise StorageUnavailableError("the connection was lost")

    def save(self, audio_hash: AudioHash, markers: CueMarkers) -> None:
        raise StorageUnavailableError("the connection was lost")

    def delete(self, audio_hash: AudioHash) -> None:
        raise StorageUnavailableError("the connection was lost")


@pytest.mark.parametrize(("method", "path", "body"), route_table(), ids=ROUTE_IDS)
async def test_a_lost_database_is_503_unavailable(
    rig: Rig, method: str, path: str, body: Body | None
) -> None:
    """R7, note 11: StorageUnavailableError from the library read is 503 ``"unavailable"``."""
    ports = CueEditorPorts(files=LostFiles(), cues=rig.cues, edits=rig.edits)
    response = await call(app_for(ports), method, path, body)
    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})


@pytest.mark.parametrize(("method", "path", "body"), route_table()[:3], ids=ROUTE_IDS[:3])
async def test_a_lost_database_under_the_edit_store_is_503_unavailable(
    rig: Rig, method: str, path: str, body: Body | None
) -> None:
    """R7, note 11: the same when the edit store is the part that fails."""
    ports = CueEditorPorts(files=rig.files, cues=rig.cues, edits=LostEdits())
    response = await call(app_for(ports), method, path, body)
    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})


# --- The token ----------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), route_table(), ids=ROUTE_IDS)
async def test_token_required(rig: Rig, method: str, path: str, body: Body | None) -> None:
    """R7, H1: without the token every route is 401, whatever the file, and a PUT or DELETE
    changes nothing."""
    rig.put_on_disk()
    rig.edits.save(HASH, CueMarkers(1_000, 200_000, 200_000, 210_000))
    before = dict(rig.edits.edits)
    response = await call(app_for(rig.ports, token=False), method, path, body)
    assert response.status_code == 401
    assert rig.edits.edits == before


@pytest.mark.parametrize(("method", "path", "body"), route_table(), ids=ROUTE_IDS)
async def test_a_wrong_token_is_401(rig: Rig, method: str, path: str, body: Body | None) -> None:
    """R7, H1: a token that is not the configured one is refused too."""
    rig.put_on_disk()
    headers = {"X-Airwave-Token": "not-" + get_settings().airwave_token}
    response = await call(app_for(rig.ports, token=False), method, path, body, headers)
    assert response.status_code == 401


async def test_the_configured_token_is_accepted(rig: Rig) -> None:
    """R7, H1: the real token check lets the configured token through."""
    headers = {"X-Airwave-Token": get_settings().airwave_token}
    response = await call(app_for(rig.ports, token=False), "GET", cues_path(), headers=headers)
    assert response.status_code == 200
