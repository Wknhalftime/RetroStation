"""The stations API rejects empty or null ``call_letters`` at the edge (I1, final review of
PR F1: ``backend/routers/stations.py``). Before the fix, an empty or null ``call_letters`` on
an update reached ``StationChanges.__post_init__`` and leaked as a 500; an empty value on
create was accepted outright. Both now answer 422, with no Pg connection needed: dependencies
are overridden with the in-memory fake repository, so these run with no DB."""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.dependencies import get_current_token, get_sync_repos
from backend.domain.broadcast import BroadcastStation
from backend.routers import stations
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository

STATIONS = "/api/v1/stations"


class _ReposStub:
    """Just enough of ``RepositoryFactory`` for the stations routes under test."""

    def __init__(self) -> None:
        self.broadcast_stations = FakeBroadcastStationRepository()


def _client(repos: _ReposStub) -> TestClient:
    app = FastAPI()
    app.include_router(stations.router, prefix="/api/v1/stations")
    app.dependency_overrides[get_sync_repos] = lambda: repos
    app.dependency_overrides[get_current_token] = lambda: "test-token"
    return TestClient(app)


def test_create_with_empty_call_letters_is_422() -> None:
    response = _client(_ReposStub()).post(STATIONS, json={"call_letters": ""})
    assert response.status_code == 422


def test_update_with_empty_call_letters_is_422_not_500() -> None:
    repos = _ReposStub()
    station = repos.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KAZR-FM"))
    response = _client(repos).put(f"{STATIONS}/{station.id}", json={"call_letters": ""})
    assert response.status_code == 422


def test_update_with_null_call_letters_is_422_not_500() -> None:
    repos = _ReposStub()
    station = repos.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KAZR-FM"))
    response = _client(repos).put(f"{STATIONS}/{station.id}", json={"call_letters": None})
    assert response.status_code == 422


def test_update_omitting_call_letters_still_succeeds() -> None:
    """A partial update that never mentions call_letters is unaffected by the edge check."""
    repos = _ReposStub()
    station = repos.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KAZR-FM"))
    response = _client(repos).put(f"{STATIONS}/{station.id}", json={"name": "New Name"})
    assert response.status_code == 200
    assert response.json()["call_letters"] == "KAZR-FM"
