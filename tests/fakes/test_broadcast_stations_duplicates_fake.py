"""The station fake refuses twins as the PostgreSQL adapter does (spec D72: twins in any case
are blocked; C1: the adapter raises the domain's error; project rule: fakes implement the
repository ABCs, so services see the same contract)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import pytest

from backend.domain.broadcast import BroadcastStation, DuplicateCallLettersError
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository


def _create_exact(stations: FakeBroadcastStationRepository) -> None:
    stations.create(BroadcastStation(id=uuid4(), call_letters="KAZR-FM"))


def _create_case(stations: FakeBroadcastStationRepository) -> None:
    stations.create(BroadcastStation(id=uuid4(), call_letters="kazr-fm"))


def _update_case(stations: FakeBroadcastStationRepository) -> None:
    kioa = stations.create(BroadcastStation(id=uuid4(), call_letters="KIOA-FM"))
    stations.update(replace(kioa, call_letters="Kazr-FM"))


@pytest.mark.parametrize(
    "twin",
    [_create_exact, _create_case, _update_case],
    ids=["create exact", "create case", "update case"],
)
def test_the_fake_refuses_a_twin(
    twin: Callable[[FakeBroadcastStationRepository], None],
) -> None:
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=uuid4(), call_letters="KAZR-FM"))
    with pytest.raises(DuplicateCallLettersError):
        twin(stations)


def test_the_fake_allows_a_station_to_change_the_case_of_its_own_call_letters() -> None:
    stations = FakeBroadcastStationRepository()
    kioa = stations.create(BroadcastStation(id=uuid4(), call_letters="KIOA-FM"))
    stations.update(replace(kioa, call_letters="Kioa-FM"))
    found = stations.get_by_id(kioa.id)
    assert found is not None
    assert found.call_letters == "Kioa-FM"
