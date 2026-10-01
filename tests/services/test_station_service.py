"""The stations use cases, moved out of the routes, and their input value (spec D72: twins in
any case are refused, stored casing is kept as typed; C1: the routes parse, call one service
function and map domain errors, so the partial update's merge lives here; house rule: value
objects validate on load and name the field)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.domain.broadcast import (
    BroadcastStation,
    DuplicateCallLettersError,
    StationChanges,
    UnknownStationError,
)
from backend.services.stations import create_station, update_station
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository


@pytest.fixture
def stations() -> FakeBroadcastStationRepository:
    return FakeBroadcastStationRepository()


def kazr(stations: FakeBroadcastStationRepository) -> BroadcastStation:
    return stations.create(
        BroadcastStation(
            id=uuid4(), call_letters="KAZR-FM", name="Laser", city="Waukee", format_name="CHR"
        )
    )


def test_creating_a_station_stores_it_as_typed(stations: FakeBroadcastStationRepository) -> None:
    draft = BroadcastStation(id=uuid4(), call_letters="Kazr-FM", name="Laser", city="Waukee")
    created = create_station(stations, draft)
    assert stations.get_by_id(draft.id) == created
    assert (created.id, created.call_letters, created.name, created.city) == (
        draft.id,
        "Kazr-FM",
        "Laser",
        "Waukee",
    )


def test_creating_a_twin_raises_duplicate_call_letters(
    stations: FakeBroadcastStationRepository,
) -> None:
    kazr(stations)
    with pytest.raises(DuplicateCallLettersError):
        create_station(stations, BroadcastStation(id=uuid4(), call_letters="kazr-fm"))


def test_updating_an_unknown_station_raises_unknown_station(
    stations: FakeBroadcastStationRepository,
) -> None:
    with pytest.raises(UnknownStationError):
        update_station(stations, uuid4(), StationChanges({"name": "Laser"}))
    assert stations.list_all() == []


def test_an_update_changes_only_the_fields_it_sets(
    stations: FakeBroadcastStationRepository,
) -> None:
    station = kazr(stations)
    updated = update_station(
        stations, station.id, StationChanges({"name": "Laser 103.3", "city": None})
    )
    assert (updated.call_letters, updated.name, updated.city, updated.format_name) == (
        "KAZR-FM",
        "Laser 103.3",
        None,
        "CHR",
    )
    assert stations.get_by_id(station.id) == updated


def test_renaming_onto_a_twin_raises_duplicate_call_letters(
    stations: FakeBroadcastStationRepository,
) -> None:
    kazr(stations)
    kioa = stations.create(BroadcastStation(id=uuid4(), call_letters="KIOA-FM"))
    with pytest.raises(DuplicateCallLettersError):
        update_station(stations, kioa.id, StationChanges({"call_letters": "kazr-fm"}))


def test_a_station_may_change_the_case_of_its_own_call_letters(
    stations: FakeBroadcastStationRepository,
) -> None:
    station = kazr(stations)
    update_station(stations, station.id, StationChanges({"call_letters": "Kazr-FM"}))
    stored = stations.get_by_id(station.id)
    assert stored is not None
    assert stored.call_letters == "Kazr-FM"


@pytest.mark.parametrize(
    ("values", "field_name"),
    [
        ({"colour": "red"}, "colour"),
        ({"call_letters": ""}, "call_letters"),
        ({"call_letters": None}, "call_letters"),
    ],
    ids=["unknown field", "empty call letters", "no call letters"],
)
def test_station_changes_refuse_a_bad_field_and_name_it(
    values: dict[str, str | None], field_name: str
) -> None:
    # House rule: value objects validate on load and name the field
    with pytest.raises(ValueError, match=rf"StationChanges\.{field_name}\b"):
        StationChanges(values)
