"""The stations use cases: create and update, with the partial-update merge moved out of the
router (spec D72: a station differing from another only by case is refused; C1: routes parse
input, call one service function, and map domain errors)."""

from __future__ import annotations

from dataclasses import replace
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
    values = changes.values
    # StationChanges.__post_init__ guarantees a present call_letters is a non-empty str, but
    # the mapping's value type is `str | None` for every field; narrow it explicitly instead
    # of a cast, so mypy still sees `BroadcastStation.call_letters: str`.
    call_letters = values.get("call_letters")
    if call_letters is None:
        call_letters = existing.call_letters
    changed = replace(
        existing,
        call_letters=call_letters,
        name=values.get("name", existing.name),
        city=values.get("city", existing.city),
        format_name=values.get("format_name", existing.format_name),
    )
    return stations.update(changed)
