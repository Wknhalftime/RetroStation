"""The streaming settings routes over HTTP (PR G1, Task 2; traceability E: T2.8-T2.13).

Requirements: Delivery row G "Settings page: max listeners" (S1); D10; D27 (refused on save,
in FastAPI's list shape); D34 and PG1 (the streaming state, read-only, from STREAM_ENABLED and
whether the engine started; the page works while streaming is off, manual check 8); H1
(under ``/api/v1``, ``X-Airwave-Token`` required); H2 (the route parses, calls one service
function, maps domain errors). The plan's route table: ``GET
/api/v1/streaming/settings`` answers exactly five keys; ``PUT /api/v1/streaming/max-sessions``
takes ``{"value": int}`` (strict, >= 1) and answers ``{"max_sessions": int}``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from backend.config import get_settings
from backend.dependencies import (
    get_current_token,
    get_sign_off_ports,
    get_streaming_state,
    get_user_settings,
)
from backend.domain.streaming import ClipFormat, ProbedClip, SignOff
from backend.routers.v1 import router as v1_router
from backend.services.streaming.sign_off import SignOffPorts
from backend.services.streaming.stream_settings import StreamingState
from tests.fakes.user_settings import FakeUserSettingRepository

SETTINGS = "/api/v1/streaming/settings"
MAX_SESSIONS = "/api/v1/streaming/max-sessions"
LIMIT_KEY = "stream_max_sessions"
SIGN_OFF_KEY = "stream_sign_off"
CLIP = SignOff(
    file_name="0123456789abcdef.mp3", format=ClipFormat.MP3, span_ms=12_500, name="Good night.mp3"
)


def no_probe(path: Path) -> ProbedClip:
    raise AssertionError(f"these routes never probe a clip ({path})")


def app_for(
    settings: FakeUserSettingRepository,
    folder: Path,
    streaming: StreamingState = StreamingState.ON,
    *,
    token: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.include_router(v1_router)
    ports = SignOffPorts(settings=settings, folder=folder, probe=no_probe, commit=lambda: None)
    app.dependency_overrides[get_user_settings] = lambda: settings
    app.dependency_overrides[get_sign_off_ports] = lambda: ports
    app.dependency_overrides[get_streaming_state] = lambda: streaming
    if token:
        app.dependency_overrides[get_current_token] = lambda: "test-token"
    return app


async def call(app: FastAPI, method: str, path: str, body: object = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        if body is None:
            return await http.request(method, path)
        return await http.request(method, path, json=body)


def stored(settings: FakeUserSettingRepository, key: str) -> str | None:
    found = settings.get(key)
    return None if found is None else found.value


async def test_the_settings_are_json_in_the_pages_shape(tmp_path: Path) -> None:
    # T2.8 (S1, PG1): exactly the five keys; the clip as name, seconds and format.
    settings = FakeUserSettingRepository({LIMIT_KEY: "5", SIGN_OFF_KEY: CLIP.to_setting()})
    (tmp_path / CLIP.file_name).write_bytes(b"ID3")
    response = await call(app_for(settings, tmp_path), "GET", SETTINGS)
    assert response.status_code == 200
    assert response.json() == {
        "streaming": "on",
        "max_sessions": 5,
        "max_sessions_problem": None,
        "sign_off": {"name": "Good night.mp3", "seconds": 12.5, "format": "mp3"},
        "sign_off_problem": None,
    }


@pytest.mark.parametrize("value", [1, 10])
async def test_putting_the_limit_stores_it_and_answers_it(value: int, tmp_path: Path) -> None:
    # T2.9 (D27, S1).
    settings = FakeUserSettingRepository()
    response = await call(app_for(settings, tmp_path), "PUT", MAX_SESSIONS, {"value": value})
    assert (response.status_code, response.json()) == (200, {"max_sessions": value})
    assert stored(settings, LIMIT_KEY) == str(value)


@pytest.mark.parametrize("value", [0, -1, "3", 2.5])
async def test_a_bad_limit_is_422_in_fastapis_shape_and_not_stored(
    value: object, tmp_path: Path
) -> None:
    # T2.10 (D27, H2): strict whole numbers >= 1 only; nothing is stored.
    settings = FakeUserSettingRepository({LIMIT_KEY: "4"})
    response = await call(app_for(settings, tmp_path), "PUT", MAX_SESSIONS, {"value": value})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert all({"loc", "msg", "type"} <= set(entry) for entry in detail)
    assert stored(settings, LIMIT_KEY) == "4"


@pytest.mark.parametrize(
    ("method", "path", "body"), [("GET", SETTINGS, None), ("PUT", MAX_SESSIONS, {"value": 2})]
)
async def test_the_streaming_settings_routes_need_the_token(
    method: str, path: str, body: object, tmp_path: Path
) -> None:
    # T2.11 (H1).
    settings = FakeUserSettingRepository()
    response = await call(app_for(settings, tmp_path, token=False), method, path, body)
    assert response.status_code == 401
    assert stored(settings, LIMIT_KEY) is None


async def test_the_settings_are_served_while_streaming_is_off(tmp_path: Path) -> None:
    # T2.12 (PG1, manual check 8): the page works with streaming off, and says so.
    settings = FakeUserSettingRepository()
    app = app_for(settings, tmp_path, StreamingState.OFF)
    read = await call(app, "GET", SETTINGS)
    assert read.status_code == 200
    assert read.json()["streaming"] == "off"
    assert read.json()["max_sessions"] == 3
    put = await call(app, "PUT", MAX_SESSIONS, {"value": 2})
    assert put.status_code == 200


@pytest.mark.parametrize(
    ("enabled", "service", "expected"),
    [("true", object(), "on"), ("true", None, "unavailable"), ("false", None, "off")],
    ids=["on", "unavailable", "off"],
)
async def test_the_page_reads_the_state_from_stream_enabled_and_the_service(
    enabled: str,
    service: object,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T2.13 (D34, PG1; audit SF2): the real state dependency, wired to STREAM_ENABLED and to
    # whether the app's stream service started (``app.state.stream_service``, set at startup).
    monkeypatch.setenv("STREAM_ENABLED", enabled)
    get_settings.cache_clear()
    try:
        app = app_for(FakeUserSettingRepository(), tmp_path)
        del app.dependency_overrides[get_streaming_state]
        app.state.stream_service = service
        response = await call(app, "GET", SETTINGS)
    finally:
        get_settings.cache_clear()
    assert (response.status_code, response.json()["streaming"]) == (200, expected)
