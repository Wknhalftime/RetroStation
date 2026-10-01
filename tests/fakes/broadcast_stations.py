from uuid import UUID

from backend.domain.broadcast import BroadcastStation, DuplicateCallLettersError
from backend.repositories.broadcast_stations import BroadcastStationRepository


class FakeBroadcastStationRepository(BroadcastStationRepository):
    """In-memory stations. Call letters match in any case, and a station whose call letters
    differ from another's only by case is refused, as the PostgreSQL adapter does (D72)."""

    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastStation] = {}

    def _refuse_twin(self, station: BroadcastStation) -> None:
        wanted = station.call_letters.lower()
        if any(
            other.id != station.id and other.call_letters.lower() == wanted
            for other in self._data.values()
        ):
            raise DuplicateCallLettersError(
                f"another station has the call letters {station.call_letters!r}"
            )

    def create(self, station: BroadcastStation) -> BroadcastStation:
        self._refuse_twin(station)
        self._data[station.id] = station
        return station

    def get_by_id(self, station_id: UUID) -> BroadcastStation | None:
        return self._data.get(station_id)

    def get_by_call_letters(self, call_letters: str) -> BroadcastStation | None:
        wanted = call_letters.lower()
        return next((s for s in self._data.values() if s.call_letters.lower() == wanted), None)

    def list_all(self) -> list[BroadcastStation]:
        return list(self._data.values())

    def update(self, station: BroadcastStation) -> BroadcastStation:
        self._refuse_twin(station)
        self._data[station.id] = station
        return station

    def delete(self, station_id: UUID) -> None:
        self._data.pop(station_id, None)
