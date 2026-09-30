"""The public stream (spec: "GET /listen/{call_letters}/{year}?key= returns the MP3 stream
through playout.relay", unauthenticated; Errors and edge cases; D14 plain HTTP errors; D25
ICY; D27; D36)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.dependencies import get_current_token
from backend.playout.relay import icy_block
from backend.routers import listen
from backend.services.streaming.bookmarks import BookmarkKey
from backend.services.streaming.service import StreamService
from tests.services.streaming.helpers import DAY, STATION, YEAR, Rig, make_rig, song

AUDIO = b"\xff\xfb\x90\x00" + bytes(range(256)) * 80  # 20 484 bytes: one ICY block at 16 000


def app_for(service: StreamService | None) -> FastAPI:
    app = FastAPI()
    app.include_router(listen.router)
    app.state.stream_service = service
    return app


async def get(
    service: StreamService | None, path: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app_for(service), client=("192.168.1.30", 50123))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get(path, headers=headers)


def with_morning(rig: Rig) -> Rig:
    rig.engines.chunks = (AUDIO,)
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    return rig


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return with_morning(make_rig(tmp_path))


async def test_the_stream_is_served_as_mp3(rig: Rig) -> None:
    response = await get(rig.service, "/listen/KIOA/1995")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert "icy-metaint" not in response.headers
    assert response.content == AUDIO


async def test_when_the_engine_stream_ends_the_session_closes(rig: Rig) -> None:
    await get(rig.service, "/listen/KIOA/1995")
    sid = rig.engines.endpoints[0].session_id
    assert rig.service.open_sessions == 0
    assert rig.engines.stopped == [sid]
    assert rig.engines.upstreams_closed == [sid]


async def test_the_listener_key_reaches_the_bookmarks(rig: Rig) -> None:
    await get(rig.service, "/listen/KIOA/1995?key=car")
    assert rig.bookmarks.get(BookmarkKey("car", STATION, YEAR)) is not None  # D30


@pytest.mark.parametrize("path", ["/listen/kioa/1995", "/listen/KXXX/1995", "/listen/KIOA/1996"])
async def test_nothing_to_play_is_404_no_broadcast(rig: Rig, path: str) -> None:
    response = await get(rig.service, path)
    assert (response.status_code, response.json()) == (404, {"detail": "no broadcast"})


async def test_a_resume_past_the_end_of_the_log_is_404_no_broadcast(rig: Rig) -> None:
    # D43: 404 once; the bookmark is dropped, so the next tune-in goes by the clock.
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:30:00"), song("06:33:20")])
    rig.clock.now = datetime(2026, 3, 14, 6, 5)  # a logged gap: lands on 06:30 from the top
    sid = await rig.open("car")
    await rig.item(sid, 0)
    rig.started(sid, 0)
    rig.clock.advance(seconds=10)
    rig.service.close(sid)
    rig.clock.advance(seconds=400)  # the radio kept playing past the last song of the log
    ended = await get(rig.service, "/listen/KIOA/1995?key=car")
    assert (ended.status_code, ended.json()) == (404, {"detail": "no broadcast"})
    assert rig.bookmarks.get(BookmarkKey("car", STATION, YEAR)) is None
    by_the_clock = await get(rig.service, "/listen/KIOA/1995?key=car")
    assert by_the_clock.status_code == 200


async def test_all_slots_taken_is_503_station_busy(tmp_path: Path) -> None:
    rig = with_morning(make_rig(tmp_path, settings={"stream_max_sessions": "1"}))
    await rig.open()
    response = await get(rig.service, "/listen/KIOA/1995")
    assert (response.status_code, response.json()) == (503, {"detail": "station busy"})


async def test_an_engine_that_will_not_start_is_503_unavailable(tmp_path: Path) -> None:
    rig = with_morning(make_rig(tmp_path, engine_fails=True))
    response = await get(rig.service, "/listen/KIOA/1995")
    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})


async def test_an_invalid_limit_setting_is_503_unavailable(tmp_path: Path) -> None:
    rig = with_morning(make_rig(tmp_path, settings={"stream_max_sessions": "0"}))
    response = await get(rig.service, "/listen/KIOA/1995")
    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})


async def test_with_streaming_off_the_answer_is_503_unavailable() -> None:
    response = await get(None, "/listen/KIOA/1995")
    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})


@pytest.mark.parametrize("year", ["0", "10000", "nineteen"])
async def test_a_year_that_is_not_a_calendar_year_is_422(rig: Rig, year: str) -> None:
    assert (await get(rig.service, f"/listen/KIOA/{year}")).status_code == 422


async def test_an_icy_client_gets_the_title_every_metaint_bytes(rig: Rig) -> None:
    response = await get(rig.service, "/listen/KIOA/1995", {"Icy-MetaData": "1"})
    assert response.headers["icy-metaint"] == "16000"
    assert response.content == AUDIO[:16_000] + icy_block("") + AUDIO[16_000:]


def test_main_serves_listen_at_the_root_without_the_api_token() -> None:
    from backend.main import app

    def calls(dependant: Any) -> list[Any]:
        found = [dependant.call]
        for sub in dependant.dependencies:
            found.extend(calls(sub))
        return found

    routes = {route.path: route for route in app.routes if isinstance(route, APIRoute)}
    route = routes["/listen/{call_letters}/{year}"]
    assert get_current_token not in calls(route.dependant)
