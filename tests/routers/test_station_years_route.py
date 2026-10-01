"""The station-year list over HTTP (spec D70: days logged only; D71: public, outside /api/v1,
no token, no secrets; D77: no cue count; D34: streaming off does not gate it; "Public
routes, unauthenticated, for LAN players"; the F2 contract's four keys)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from backend.dependencies import get_current_token, get_station_year_repos
from backend.domain.broadcast import BroadcastStation
from backend.routers import radio
from backend.services.streaming.station_years import StationYearRepos
from tests.fakes.broadcast_days import FakeBroadcastDayRepository
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository

LIST = "/radio/station-years"


@pytest.fixture
def repos() -> StationYearRepos:
    return StationYearRepos(
        stations=FakeBroadcastStationRepository(), days=FakeBroadcastDayRepository()
    )


def app_for(repos: StationYearRepos) -> FastAPI:
    app = FastAPI()
    app.include_router(radio.router)
    app.dependency_overrides[get_station_year_repos] = lambda: repos
    app.state.stream_service = None  # streaming off (D34)
    return app


async def get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, client=("192.168.1.30", 50123))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get(path)


async def test_the_list_is_json_in_the_contracts_shape(repos: StationYearRepos) -> None:
    kioa = repos.stations.create(BroadcastStation(id=uuid4(), call_letters="KIOA"))
    repos.days.get_or_create(kioa.id, date(1995, 3, 14))
    repos.days.get_or_create(kioa.id, date(1995, 3, 15))
    response = await get(app_for(repos), LIST)
    assert response.status_code == 200
    assert response.json() == [
        {"call_letters": "KIOA", "year": 1995, "days_logged": 2, "days_in_year": 365}
    ]


async def test_an_empty_library_is_an_empty_list(repos: StationYearRepos) -> None:
    response = await get(app_for(repos), LIST)
    assert (response.status_code, response.json()) == (200, [])


async def test_the_list_is_served_while_streaming_is_off(repos: StationYearRepos) -> None:
    app = app_for(repos)
    assert app.state.stream_service is None
    assert (await get(app, LIST)).status_code == 200


def test_main_serves_the_list_at_the_root_without_the_api_token() -> None:
    from backend.main import app

    def calls(dependant: Dependant) -> list[Callable[..., object] | None]:
        found: list[Callable[..., object] | None] = [dependant.call]
        for sub in dependant.dependencies:
            found.extend(calls(sub))
        return found

    routes = {route.path: route for route in app.routes if isinstance(route, APIRoute)}
    route = routes[LIST]
    assert get_current_token not in calls(route.dependant)
