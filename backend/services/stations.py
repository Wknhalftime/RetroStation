"""The stations use cases: create and update, with the partial-update merge moved out of the
router (spec D72: a station differing from another only by case is refused; C1: routes parse
input, call one service function, and map domain errors)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast
from uuid import UUID

from backend.domain.broadcast import BroadcastStation, StationChanges, UnknownStationError
from backend.repositories.broadcast_stations import BroadcastStationRepository


def create_station(
    stations: BroadcastStationRepository, draft: BroadcastStation
) -> BroadcastStation:
    """Store ``draft`` as typed and return the stored row.

    Propagates `DuplicateCallLettersError` when another station already has these call
    letters, in any case (D72).
    """
    return stations.create(draft)


def update_station(
    stations: BroadcastStationRepository, station_id: UUID, changes: StationChanges
) -> BroadcastStation:
    """Apply a partial update and return the stored row; unset fields keep their values.

    Raises `UnknownStationError` when no station has ``station_id``. Propagates
    `DuplicateCallLettersError` when the change collides with another station's call
    letters, in any case (D72).
    """
    existing = stations.get_by_id(station_id)
    if existing is None:
        raise UnknownStationError(f"no station has id {station_id}")
    # dataclasses.replace's stub checks each kwarg against the field it names, which mypy
    # cannot do for an arbitrary, dynamically-keyed mapping validated at runtime instead
    # (StationChanges.__post_init__): the cast says so explicitly, once, here.
    changed = replace(existing, **cast(dict[str, Any], changes.values))
    return stations.update(changed)
