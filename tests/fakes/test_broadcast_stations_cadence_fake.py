"""Station liners, PR L1 Task 2: ``FakeBroadcastStationRepository`` keeps the cadence rules
the PostgreSQL adapter keeps (``tests/integration/test_station_liners_pg.py``).

Requirement LR1: the cadence is set and cleared with ``set_liner_cadence``, read back on the
station, and ``update()`` (editing the station's details) leaves it alone.
"""

from __future__ import annotations

from uuid import uuid4

from backend.domain.broadcast import BroadcastStation
from backend.domain.liners import LinerCadence
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository


def test_fake_cadence_round_trip() -> None:
    """LR1: set 3, read it on the station (by id and in the list); None turns it off."""
    stations = FakeBroadcastStationRepository()
    station_id = uuid4()
    stations.create(BroadcastStation(station_id, "WKRP"))

    stations.set_liner_cadence(station_id, LinerCadence(3))
    station = stations.get_by_id(station_id)
    assert station is not None and station.liner_cadence == LinerCadence(3)
    assert [s.liner_cadence for s in stations.list_all()] == [LinerCadence(3)]

    stations.set_liner_cadence(station_id, None)
    cleared = stations.get_by_id(station_id)
    assert cleared is not None and cleared.liner_cadence is None


def test_fake_update_station_keeps_cadence() -> None:
    """LR1: editing the station's details leaves its cadence alone, whatever the station
    object passed to ``update`` carries."""
    stations = FakeBroadcastStationRepository()
    station_id = uuid4()
    stations.create(BroadcastStation(station_id, "WKRP"))
    stations.set_liner_cadence(station_id, LinerCadence(5))

    updated = stations.update(BroadcastStation(station_id, "WKRP", name="WKRP in Cincinnati"))
    read = stations.get_by_id(station_id)

    assert updated.liner_cadence == LinerCadence(5)
    assert read is not None
    assert (read.name, read.liner_cadence) == ("WKRP in Cincinnati", LinerCadence(5))
