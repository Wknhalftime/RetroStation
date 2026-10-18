"""Station liners, PR L3 Task 15: a liner's cues over HTTP, by liner id.

Requirements: LR11 (the cue editor opens a liner by its id; Fade-out is pinned to Stop and the
liner API never takes a fade; Save checks the liner rule and Stop <= the liner's length), LR4
(edits are per liner; a silent liner is refused; no D9 minimum), LR3 (another station's liner
is not found). Under ``/api/v1/stations``, ``X-Airwave-Token`` required, plain ``def``s:

    GET    /{station_id}/liners/{liner_id}/cues      200, the library editor's CueEditorOut
    PUT    /{station_id}/liners/{liner_id}/cues      body {start_ms, eom_ms, stop_ms}, 204
    DELETE /{station_id}/liners/{liner_id}/cues      204
    GET    /{station_id}/liners/{liner_id}/waveform  200 {duration_ms, per_second, peaks}

Errors: unknown station or liner, or another station's liner, 404; a disordered body or a
refused edit 422 in FastAPI's list shape at ``loc ["body"]`` with the reason; unreadable audio
422; a lost database 503 ``"unavailable"``. ``get_liner_cue_ports`` and ``get_peak_reader`` are
overridden with fakes through ``app.dependency_overrides``.
"""

from __future__ import annotations

import base64
import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_type_hints
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.config import get_settings
from backend.dependencies import (
    SyncRepos,
    get_current_token,
    get_liner_cue_ports,
    get_peak_reader,
)
from backend.domain.library import AudioHash
from backend.domain.liners import LinerStorageError, StationLiner
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    ClipFormat,
    CueAnalysis,
    CueMarkers,
    CuePoints,
)
from backend.domain.system import StorageUnavailableError
from backend.playout.peaks import PeakReadError, Peaks
from backend.routers.v1 import router as v1_router
from backend.services.repository_factory import RepositoryFactory
from backend.services.streaming.liner_cues import LinerCuePorts
from backend.services.streaming.liners import liner_folder
from tests.fakes.station_liners import FakeStationLiners
from tests.fakes.stream_cues import FakeStreamCueRepository

STATIONS = "/api/v1/stations"
STATION_ID = UUID("0a1b2c3d-4e5f-4a6b-9c7d-8e9fa0b1c2d3")
OTHER_STATION_ID = UUID("1b2c3d4e-5f60-4b7c-8d9e-0fa1b2c3d4e5")
LINER_ID = UUID("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")
TWIN_ID = UUID("7a2d3e4f-5b6c-4d7e-9f80-123456789abc")
HASH = AudioHash.parse("audio-sha256:" + "a" * 64)
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
DURATION_MS = 7_000
MEASURED = CuePoints(120, 6_900, 0, 400, 300, -3.5)
EDIT = {"start_ms": 500, "eom_ms": 6_000, "stop_ms": 6_800}
EDIT_OUT = {"start_ms": 500, "eom_ms": 6_000, "fade_at_ms": 6_800, "stop_ms": 6_800}
SILENT_REFUSAL = "This liner was measured silent, so the radio skips it; its cues can't be edited."
PAST_THE_END = "Stop can't be past the end of the liner (0:07.000)."
LIBRARY_VIEW_KEYS = {
    "file_id",
    "title",
    "artist",
    "format",
    "duration_ms",
    "analysis",
    "automatic",
    "manual",
}

Body = dict[str, object]
Route = tuple[str, str, Body | None]


def liner_path(station_id: UUID = STATION_ID, liner_id: UUID = LINER_ID) -> str:
    return f"{STATIONS}/{station_id}/liners/{liner_id}"


def cues_path(station_id: UUID = STATION_ID, liner_id: UUID = LINER_ID) -> str:
    return f"{liner_path(station_id, liner_id)}/cues"


def waveform_path(station_id: UUID = STATION_ID, liner_id: UUID = LINER_ID) -> str:
    return f"{liner_path(station_id, liner_id)}/waveform"


def route_table(station_id: UUID = STATION_ID, liner_id: UUID = LINER_ID) -> list[Route]:
    """The four routes, each with a body that would be valid for the stored liner."""
    return [
        ("GET", cues_path(station_id, liner_id), None),
        ("PUT", cues_path(station_id, liner_id), EDIT),
        ("DELETE", cues_path(station_id, liner_id), None),
        ("GET", waveform_path(station_id, liner_id), None),
    ]


ROUTE_IDS = ["get-cues", "put-cues", "delete-cues", "waveform"]


def station_liner(
    liner_id: UUID = LINER_ID,
    *,
    station_id: UUID = STATION_ID,
    duration_ms: int = DURATION_MS,
    markers: CueMarkers | None = None,
) -> StationLiner:
    return StationLiner(
        id=liner_id,
        station_id=station_id,
        name="Station ID",
        file_name=f"{liner_id}.mp3",
        format=ClipFormat.MP3,
        audio_hash=HASH,
        duration_ms=duration_ms,
        markers=markers,
        uploaded_at=NOW,
    )


@dataclass
class World:
    """The routes' world: the liner fake over the cue fake, a liners root, a counting commit
    (raising ``commit_error`` when set) and a recording peak reader."""

    root: Path
    cues: FakeStreamCueRepository = field(default_factory=FakeStreamCueRepository)
    commits: int = 0
    commit_error: Exception | None = None
    read: list[str] = field(default_factory=list)
    liners: FakeStationLiners = field(init=False)

    def __post_init__(self) -> None:
        self.liners = FakeStationLiners(self.cues)
        self.liners.add(station_liner())

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    def reader(self, path: str) -> Peaks:
        self.read.append(path)
        return Peaks(200, b"\x00\xff")

    def ports(self, liners: FakeStationLiners | None = None) -> LinerCuePorts:
        return LinerCuePorts(
            liners=self.liners if liners is None else liners,
            cues=self.cues,
            folder=self.root,
            commit=self.commit,
        )

    def app(
        self,
        *,
        token: bool = True,
        liners: FakeStationLiners | None = None,
        reader: Any = None,
    ) -> FastAPI:
        app = FastAPI()
        app.include_router(v1_router)
        ports = self.ports(liners)
        read = self.reader if reader is None else reader
        app.dependency_overrides[get_liner_cue_ports] = lambda: ports
        app.dependency_overrides[get_peak_reader] = lambda: read
        if token:
            app.dependency_overrides[get_current_token] = lambda: "test-token"
        return app

    def measure(self, points: CuePoints = MEASURED, *, failed: bool = False) -> None:
        self.cues.upsert(
            CueAnalysis(HASH, points, None if failed else -14.0, failed, CUE_ANALYSER_VERSION)
        )

    def measure_silent(self) -> None:
        fallback = CuePoints(0, 6_500, 0, 4_000, 0, -8.0)
        self.cues.upsert(
            CueAnalysis(HASH, fallback, None, True, CUE_ANALYSER_VERSION, audio_silent=True)
        )

    def stored(self, liner_id: UUID = LINER_ID) -> CueMarkers | None:
        liner = self.liners.get(liner_id)
        assert liner is not None
        return liner.markers

    def put_on_disk(self, liner_id: UUID = LINER_ID, station_id: UUID = STATION_ID) -> Path:
        folder = self.root / str(station_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{liner_id}.mp3"
        path.write_bytes(b"ID3audio")
        return path


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "work" / "liners")


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
    return cast(list[dict[str, Any]], detail)


class LostLiners(FakeStationLiners):
    """The liner store with its connection gone."""

    def get(self, liner_id: UUID) -> StationLiner | None:
        raise StorageUnavailableError("the connection was lost")

    def save_markers(self, liner_id: UUID, markers: CueMarkers | None) -> None:
        raise StorageUnavailableError("the connection was lost")


# --- The route table and the wiring (LR11) -------------------------------------------------


def test_the_four_routes_exist_and_are_plain_defs(world: World) -> None:
    """LR11: exactly the table's methods and paths under /api/v1/stations, none of them
    ``async def`` (the repositories are synchronous)."""
    found = {
        (method, route.path): route.endpoint
        for route in world.app().routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    base = f"{STATIONS}/{{station_id}}/liners/{{liner_id}}"
    wanted = {
        ("GET", f"{base}/cues"),
        ("PUT", f"{base}/cues"),
        ("DELETE", f"{base}/cues"),
        ("GET", f"{base}/waveform"),
    }
    assert wanted <= set(found)
    assert not [key for key in wanted if inspect.iscoroutinefunction(found[key])]


def test_the_ports_use_the_requests_liners_cues_commit_and_the_liners_root() -> None:
    """LR11: ``get_liner_cue_ports`` wires the request's liner and cue repositories, the
    request's commit, and the liners root under ``STREAM_WORK_DIR`` (the folder the audio route
    and the cue task use)."""
    liners = FakeStationLiners()
    cues = FakeStreamCueRepository()
    committed: list[str] = []
    repos = SimpleNamespace(
        broadcast=SimpleNamespace(liners=liners),
        streaming=SimpleNamespace(cues=cues),
        commit=lambda: committed.append("commit"),
    )
    ports = get_liner_cue_ports(cast(RepositoryFactory, repos))
    assert isinstance(ports, LinerCuePorts)
    assert ports.liners is liners
    assert ports.cues is cues
    assert ports.folder == liner_folder(get_settings().stream_work_dir)
    ports.commit()
    assert committed == ["commit"]


def test_the_ports_take_the_request_scoped_repositories() -> None:
    """LR11: the provider takes exactly one parameter, the request-scoped ``SyncRepos``: one
    connection per request, committed before the response (its name is free, audit M5)."""
    hints = get_type_hints(get_liner_cue_ports, include_extras=True)
    assert [hint for name, hint in hints.items() if name != "return"] == [SyncRepos]


def test_the_ports_commit_turns_a_lost_connection_into_a_domain_error() -> None:
    """LR11, plan note 15 (audit I3): a commit that meets a lost connection raises the domain's
    storage error (503 at the route), never psycopg's OperationalError (a 500); routers never
    see driver errors."""

    def lost() -> None:
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    repos = SimpleNamespace(
        broadcast=SimpleNamespace(liners=FakeStationLiners()),
        streaming=SimpleNamespace(cues=FakeStreamCueRepository()),
        commit=lost,
    )
    ports = get_liner_cue_ports(cast(RepositoryFactory, repos))
    with pytest.raises((LinerStorageError, StorageUnavailableError)):
        ports.commit()


# --- GET the view (LR11) -------------------------------------------------------------------


async def test_get_view_shape(world: World) -> None:
    """LR11: the view is exactly the library editor's shape: the liner's id, its name as the
    title, no artist, its format and length; the markers carry ``fade_at_ms == stop_ms``."""
    world.measure()
    response = await call(world.app(), "GET", cues_path())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == LIBRARY_VIEW_KEYS
    assert body == {
        "file_id": str(LINER_ID),
        "title": "Station ID",
        "artist": None,
        "format": "mp3",
        "duration_ms": DURATION_MS,
        "analysis": "measured",
        "automatic": {"start_ms": 120, "eom_ms": 6_600, "fade_at_ms": 6_900, "stop_ms": 6_900},
        "manual": None,
    }


@pytest.mark.parametrize(
    ("state", "automatic"),
    [
        ("failed", {"start_ms": 0, "eom_ms": 6_000, "fade_at_ms": 6_000, "stop_ms": 6_000}),
        ("waiting", {"start_ms": 0, "eom_ms": 7_000, "fade_at_ms": 7_000, "stop_ms": 7_000}),
        ("silent", {"start_ms": 0, "eom_ms": 7_000, "fade_at_ms": 7_000, "stop_ms": 7_000}),
    ],
)
async def test_get_view_says_how_the_liner_was_analysed(
    world: World, state: str, automatic: Body
) -> None:
    """LR11, LR4: a failed row opens on that row without a fade; no row and a silent liner open
    on the whole file."""
    if state == "failed":
        world.measure(CuePoints(0, 6_000, 0, 4_000, 0, -8.0), failed=True)
    elif state == "silent":
        world.measure_silent()
    body = (await call(world.app(), "GET", cues_path())).json()
    assert body["analysis"] == state
    assert body["automatic"] == automatic


# --- PUT save (LR11, LR4) ------------------------------------------------------------------


async def test_put_then_get_shows_manual(world: World) -> None:
    """LR11, LR4: a valid edit answers 204 with no body, is stored on the liner and committed,
    and the view then shows it as manual with Fade-out at Stop."""
    app = world.app()
    response = await call(app, "PUT", cues_path(), EDIT)
    assert response.status_code == 204
    assert response.content == b""
    assert world.stored() == CueMarkers(500, 6_000, 6_800, 6_800)
    assert world.commits == 1
    assert (await call(app, "GET", cues_path())).json()["manual"] == EDIT_OUT


async def test_put_saves_a_400_ms_liner(world: World) -> None:
    """LR4: no D9 minimum over HTTP either: a 400 ms liner saves (0, 300, 400)."""
    world.liners.liners[LINER_ID] = station_liner(duration_ms=400)
    body = {"start_ms": 0, "eom_ms": 300, "stop_ms": 400}
    assert (await call(world.app(), "PUT", cues_path(), body)).status_code == 204
    assert world.stored() == CueMarkers(0, 300, 400, 400)


@pytest.mark.parametrize(
    "changes",
    [{"eom_ms": 500}, {"eom_ms": 400}, {"eom_ms": 6_900}],
    ids=["eom-at-start", "eom-before-start", "eom-after-stop"],
)
async def test_put_bad_order_is_422(world: World, changes: Body) -> None:
    """LR11: markers that break ``start < eom <= stop`` answer 422 in the list shape at
    ``loc ["body"]`` with a reason; nothing is stored or committed."""
    response = await call(world.app(), "PUT", cues_path(), {**EDIT, **changes})
    detail = assert_list_shape(response)
    assert detail[0]["loc"] == ["body"]
    assert detail[0]["msg"]
    assert world.stored() is None
    assert world.commits == 0


async def test_put_past_the_end_is_422_with_the_reason(world: World) -> None:
    """LR11: Stop past the liner's 7_000 ms answers 422 at ``loc ["body"]`` and the reason is
    the service's sentence; nothing is stored."""
    past = {"start_ms": 0, "eom_ms": 6_000, "stop_ms": 7_001}
    detail = assert_list_shape(await call(world.app(), "PUT", cues_path(), past))
    assert detail[0]["loc"] == ["body"]
    assert detail[0]["msg"] == PAST_THE_END
    assert world.stored() is None
    assert world.commits == 0


async def test_put_on_a_silent_liner_is_422_with_the_reason(world: World) -> None:
    """LR4, LR11: a silent liner's edit is refused, 422 at ``loc ["body"]`` with the reason."""
    world.measure_silent()
    detail = assert_list_shape(await call(world.app(), "PUT", cues_path(), EDIT))
    assert detail[0]["loc"] == ["body"]
    assert detail[0]["msg"] == SILENT_REFUSAL
    assert world.stored() is None


async def test_put_with_a_fade_is_refused(world: World) -> None:
    """LR11: the liner API never takes a fade: a body carrying ``fade_at_ms`` is 422 (extra
    fields are forbidden), even one equal to Stop; nothing is stored."""
    for fade_at_ms in (6_800, 6_000):
        response = await call(world.app(), "PUT", cues_path(), {**EDIT, "fade_at_ms": fade_at_ms})
        assert_list_shape(response)
    assert world.stored() is None
    assert world.commits == 0


@pytest.mark.parametrize(
    "change",
    [
        {"start_ms": "500"},
        {"start_ms": 500.5},
        {"start_ms": 500.0},
        {"start_ms": True},
        {"start_ms": None},
        {"stop_ms": "6800"},
        {"start_ms": -1},
    ],
    ids=["string", "fraction", "whole-float", "bool", "null", "string-stop", "negative"],
)
async def test_put_non_int_is_422(world: World, change: Body) -> None:
    """LR11: each marker is a strict whole number >= 0; anything else is 422, nothing stored."""
    assert_list_shape(await call(world.app(), "PUT", cues_path(), {**EDIT, **change}))
    assert world.stored() is None


@pytest.mark.parametrize("missing", ["start_ms", "eom_ms", "stop_ms"])
async def test_put_missing_marker_is_422(world: World, missing: str) -> None:
    """LR11: all three markers are required."""
    partial = {key: value for key, value in EDIT.items() if key != missing}
    assert_list_shape(await call(world.app(), "PUT", cues_path(), partial))
    assert world.stored() is None


async def test_put_leaves_another_stations_twin_alone(world: World) -> None:
    """LR4: the edit lands on this liner only, not on another station's liner with the same
    audio."""
    world.liners.add(station_liner(TWIN_ID, station_id=OTHER_STATION_ID))
    assert (await call(world.app(), "PUT", cues_path(), EDIT)).status_code == 204
    assert world.stored(TWIN_ID) is None


# --- DELETE reset (LR11, LR4) --------------------------------------------------------------


async def test_delete_clears(world: World) -> None:
    """LR11, LR4: reset answers 204 with no body, clears the liner's edit, commits, and the
    view shows no manual."""
    world.liners.save_markers(LINER_ID, CueMarkers(500, 6_000, 6_800, 6_800))
    app = world.app()
    response = await call(app, "DELETE", cues_path())
    assert response.status_code == 204
    assert response.content == b""
    assert world.stored() is None
    assert world.commits == 1
    assert (await call(app, "GET", cues_path())).json()["manual"] is None


async def test_delete_without_an_edit_is_204(world: World) -> None:
    """LR11: resetting a liner that has no edit is not an error."""
    assert (await call(world.app(), "DELETE", cues_path())).status_code == 204


# --- The waveform (LR11) -------------------------------------------------------------------


async def test_waveform_base64(world: World) -> None:
    """LR11: the liner's peaks as base64 (b"\\x00\\xff" is "AP8="), its length and the reader's
    rate; the reader is given the liner's stored file."""
    path = world.put_on_disk()
    response = await call(world.app(), "GET", waveform_path())
    assert response.status_code == 200
    assert response.json() == {"duration_ms": DURATION_MS, "per_second": 200, "peaks": "AP8="}
    assert base64.b64decode(response.json()["peaks"]) == b"\x00\xff"
    assert [Path(target) for target in world.read] == [path]


async def test_waveform_unreadable_422(world: World) -> None:
    """LR11: audio the reader cannot decode is 422, not a 500."""
    world.put_on_disk()

    def broken(path: str) -> Peaks:
        raise PeakReadError("Invalid data found when processing input")

    response = await call(world.app(reader=broken), "GET", waveform_path())
    assert response.status_code == 422


async def test_waveform_file_gone_is_404(world: World) -> None:
    """LR11: the row exists but the stored file is gone: 404, and the reader never runs."""
    response = await call(world.app(), "GET", waveform_path())
    assert response.status_code == 404
    assert world.read == []


# --- Not found (LR3, LR11) -----------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), route_table(liner_id=uuid4()), ids=ROUTE_IDS)
async def test_unknown_liner_404(world: World, method: str, path: str, body: Body | None) -> None:
    """LR11: a liner id the station does not have is 404 on every route."""
    assert (await call(world.app(), method, path, body)).status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), route_table(uuid4()), ids=ROUTE_IDS)
async def test_unknown_station_404(world: World, method: str, path: str, body: Body | None) -> None:
    """LR11: an unknown station is 404 on every route."""
    assert (await call(world.app(), method, path, body)).status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), route_table(OTHER_STATION_ID), ids=ROUTE_IDS)
async def test_another_stations_liner_404(
    world: World, method: str, path: str, body: Body | None
) -> None:
    """LR3, LR11: a liner asked for under another station is 404 on every route, even with its
    file on disk; its edit is untouched and nothing is committed."""
    edit = CueMarkers(500, 6_000, 6_800, 6_800)
    world.liners.save_markers(LINER_ID, edit)
    world.put_on_disk()
    world.put_on_disk(station_id=OTHER_STATION_ID)
    assert (await call(world.app(), method, path, body)).status_code == 404
    assert world.stored() == edit
    assert world.commits == 0
    assert world.read == []


# --- A lost database (LR11) ----------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), route_table(), ids=ROUTE_IDS)
async def test_a_lost_database_is_503_unavailable(
    world: World, method: str, path: str, body: Body | None
) -> None:
    """LR11: StorageUnavailableError from the liner store is 503 ``"unavailable"``."""
    world.put_on_disk()
    response = await call(world.app(liners=LostLiners(world.cues)), method, path, body)
    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})


@pytest.mark.parametrize(
    "lost",
    [
        StorageUnavailableError("the connection was lost"),
        LinerStorageError("the liner could not be saved: the connection was lost"),
    ],
    ids=["storage-unavailable", "liner-storage"],
)
@pytest.mark.parametrize(("method", "path", "body"), route_table()[1:3], ids=ROUTE_IDS[1:3])
async def test_a_lost_commit_is_503_unavailable(
    world: World, method: str, path: str, body: Body | None, lost: Exception
) -> None:
    """LR11, plan note 15: a commit that meets a lost connection stored nothing: 503, whether
    the commit says so as ``StorageUnavailableError`` or as the liner store's own
    ``LinerStorageError`` (the request commit L1 wires for liners). Only the former's body is
    the routers' ``"unavailable"``; L1 answers ``LinerStorageError`` with its own message, so
    that body is left to the builder (audit I2)."""
    world.commit_error = lost
    response = await call(world.app(), method, path, body)
    assert response.status_code == 503
    if isinstance(lost, StorageUnavailableError):
        assert response.json() == {"detail": "unavailable"}


# --- The token (LR11) ----------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), route_table(), ids=ROUTE_IDS)
async def test_token_required(world: World, method: str, path: str, body: Body | None) -> None:
    """LR11, H1: without the token every route is 401, and a PUT or DELETE changes nothing."""
    world.put_on_disk()
    edit = CueMarkers(1_000, 5_000, 6_000, 6_000)
    world.liners.save_markers(LINER_ID, edit)
    response = await call(world.app(token=False), method, path, body)
    assert response.status_code == 401
    assert world.stored() == edit
    assert world.commits == 0


@pytest.mark.parametrize(("method", "path", "body"), route_table(), ids=ROUTE_IDS)
async def test_a_wrong_token_is_401(
    world: World, method: str, path: str, body: Body | None
) -> None:
    """LR11, H1: a token that is not the configured one is refused too."""
    world.put_on_disk()
    headers = {"X-Airwave-Token": "not-" + get_settings().airwave_token}
    response = await call(world.app(token=False), method, path, body, headers)
    assert response.status_code == 401


async def test_the_configured_token_is_accepted(world: World) -> None:
    """LR11, H1: the real token check lets the configured token through."""
    headers = {"X-Airwave-Token": get_settings().airwave_token}
    response = await call(world.app(token=False), "GET", cues_path(), headers=headers)
    assert response.status_code == 200
