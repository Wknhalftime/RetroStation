from uuid import UUID

from backend.domain.broadcast import BroadcastStation
from backend.repositories.broadcast_stations import BroadcastStationRepository


class FakeBroadcastStationRepository(BroadcastStationRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastStation] = {}

    def create(self, station: BroadcastStation) -> BroadcastStation:
        self._data[station.id] = station
        return station

    def get_by_id(self, id: UUID) -> BroadcastStation | None:
        return self._data.get(id)

    def get_by_call_letters(self, call_letters: str) -> BroadcastStation | None:
        return next((s for s in self._data.values() if s.call_letters == call_letters), None)

    def list_all(self) -> list[BroadcastStation]:
        return list(self._data.values())

    def update(self, station: BroadcastStation) -> BroadcastStation:
        self._data[station.id] = station
        return station

    def delete(self, id: UUID) -> None:
        self._data.pop(id, None)

