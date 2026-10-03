"""The station fake keeps the port's any-case lookup, so service tests can rely on it (spec
D72: call letters match in any case; project rule: fakes implement the repository ABCs)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.domain.broadcast import BroadcastStation
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository


@pytest.fixture
def stations() -> FakeBroadcastStationRepository:
    fake = FakeBroadcastStationRepository()
    fake.create(BroadcastStation(id=uuid4(), call_letters="KIOA"))
    return fake


@pytest.mark.parametrize("asked", ["KIOA", "kioa", "Kioa"])
def test_the_fake_finds_call_letters_whatever_the_case(
    stations: FakeBroadcastStationRepository, asked: str
) -> None:
    found = stations.get_by_call_letters(asked)
    assert found is not None
    assert found.call_letters == "KIOA"  # the stored casing


@pytest.mark.parametrize("asked", ["KIO", "KIOA-FM"])
def test_the_fake_finds_no_station_for_other_call_letters(
    stations: FakeBroadcastStationRepository, asked: str
) -> None:
    assert stations.get_by_call_letters(asked) is None
