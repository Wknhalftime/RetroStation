from datetime import date
from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastDay
from backend.repositories.broadcast_days import BroadcastDayRepository


class FakeBroadcastDayRepository(BroadcastDayRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastDay] = {}

    def get_or_create(self, station_id: UUID, broadcast_date: date) -> BroadcastDay:
        existing = next(
            (d for d in self._data.values()
             if d.station_id == station_id and d.broadcast_date == broadcast_date),
            None,
        )
        if existing:
            return existing
        new = BroadcastDay(id=uuid4(), station_id=station_id, broadcast_date=broadcast_date)
        self._data[new.id] = new
        return new

    def get_dates_for_station(self, station_id: UUID) -> list[date]:
        return sorted(
            d.broadcast_date for d in self._data.values() if d.station_id == station_id
        )

    def get_by_id(self, id: UUID) -> BroadcastDay | None:
        return self._data.get(id)
