from uuid import UUID

from backend.domain.broadcast import Station
from backend.repositories.stations import StationRepository


class FakeStationRepository(StationRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, Station] = {}

    def create(self, station: Station) -> Station:
        self._data[station.id] = station
        return station

    def get_by_id(self, id: UUID) -> Station | None:
        return self._data.get(id)

    def get_by_call_letters(self, call_letters: str) -> Station | None:
        return next((s for s in self._data.values() if s.call_letters == call_letters), None)

    def list_all(self) -> list[Station]:
        return list(self._data.values())

    def update(self, station: Station) -> Station:
        self._data[station.id] = station
        return station

    def delete(self, id: UUID) -> None:
        self._data.pop(id, None)
