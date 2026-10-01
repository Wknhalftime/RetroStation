"""A tune-in or a subscription whose reads fail answers ``503 unavailable`` (D88, user
2026-10-01; D14 plain HTTP errors; D78a the page is told why on its channel).

``StreamReadError`` is what the stream service's reads raise when the database cannot answer in
time or at all (``backend.db.stream_reads``); here the repository fakes raise it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import NoReturn
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from backend.domain.streaming import StreamReadError
from backend.routers import listen
from backend.services.streaming.listener_events import Status, StatusKind
from backend.services.streaming.service import StreamService
from tests.routers.sse_drive import SseDrive
from tests.services.streaming.events_rig import EventsRig, drain, make_events_rig
from tests.services.streaming.helpers import DAY, STATION, song

LISTEN = "/listen/KIOA/1995"
EVENTS = "/listen/KIOA/1995/events"
UNAVAILABLE = (503, {"detail": "unavailable"})


def app_for(service: StreamService) -> FastAPI:
    app = FastAPI()
    app.include_router(listen.router)
    app.state.stream_service = service
    return app


async def get(service: StreamService, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app_for(service), client=("192.168.1.30", 50123))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get(path)


def unreadable_day(station_id: UUID, day: date) -> NoReturn:
    raise StreamReadError("canceling statement due to lock timeout")


def unreadable_station(call_letters: str) -> NoReturn:
    raise StreamReadError("connection timeout expired")


@pytest.fixture
def rig(tmp_path: Path) -> EventsRig:
    rig = make_events_rig(tmp_path)
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    return rig


async def test_a_schedule_that_cannot_be_read_is_503_unavailable(
    rig: EventsRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rig.schedule, "get_day", unreadable_day)
    response = await get(rig.service, f"{LISTEN}?key=car")
    assert (response.status_code, response.json()) == UNAVAILABLE
    assert rig.service.open_sessions == 0  # the slot is freed
    assert rig.engines.stopped == [rig.engines.endpoints[0].session_id]  # nothing left running


async def test_a_station_that_cannot_be_read_is_503_unavailable(
    rig: EventsRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rig.stations, "get_by_call_letters", unreadable_station)
    response = await get(rig.service, LISTEN)
    assert (response.status_code, response.json()) == UNAVAILABLE


async def test_a_subscription_whose_station_cannot_be_read_is_503_unavailable(
    rig: EventsRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rig.stations, "get_by_call_letters", unreadable_station)
    async with SseDrive(app_for(rig.service), EVENTS, "key=car") as drive:
        result = await drive.result()
    assert (result.status, result.json) == UNAVAILABLE


async def test_the_page_is_told_unavailable(
    rig: EventsRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D78a: a browser <audio> element cannot read the 503, so the channel says why
    events = await rig.subscribe("car")
    monkeypatch.setattr(rig.schedule, "get_day", unreadable_day)
    with pytest.raises(StreamReadError):
        await rig.open("car")
    assert await drain(events) == [Status(StatusKind.TUNING), Status(StatusKind.UNAVAILABLE)]
